import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .config import Settings
from .database import Database
from .telegram import send_text
from .ui import ANNOUNCEMENTS, CANCEL, TEXT_LIMIT, GroupChoice, chunks, group_picker, keyboard, menu, text_length

logger = logging.getLogger(__name__)
SEND = "Отправить"


class BroadcastFlow(StatesGroup):
    group = State()
    text = State()
    confirm = State()


class Broadcasts:
    def __init__(self, db: Database):
        self.db = db
        self.wakeup = asyncio.Event()

    def publish(self, text: str, group_id: int | None, author_id: int) -> int:
        count = self.db.publish(text, group_id, author_id)
        self.wakeup.set()
        return count

    async def run(self, bot: Bot):
        while True:
            try:
                self.db.recover_deliveries()
                self.wakeup.clear()
                announcement = self.db.next_announcement()
                if announcement is None:
                    await self.wakeup.wait()
                    continue
                await self.deliver(bot, announcement)
            except Exception:
                logger.exception("Ошибка в очереди рассылок")
                await asyncio.sleep(3)

    async def deliver(self, bot, announcement):
        announcement_id = announcement["id"]
        audience = announcement["name"] or "Колледж"
        text = f"Объявление · {audience}\n\n{announcement['text']}"
        while (user_id := self.db.claim_delivery(announcement_id)) is not None:
            status = await send_text(bot, user_id, text)
            self.db.finish_delivery(announcement_id, user_id, status)
            await asyncio.sleep(0.05)
        counts = self.db.delivery_counts(announcement_id)
        text = f"Доставлено {counts.get('sent', 0)} из {sum(counts.values())}"
        if counts.get("unknown", 0):
            text += f". Не удалось проверить доставку: {counts['unknown']}."
        try:
            await send_text(bot, announcement["author_id"], text)
        finally:
            self.db.mark_reported(announcement_id)


def broadcast_router(settings: Settings, db: Database, broadcasts: Broadcasts) -> Router:
    router = Router()
    router.message.filter(F.chat.type == ChatType.PRIVATE, F.from_user)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    def picker(user_id: int, page: int = 0):
        groups = [group for group in db.groups() if settings.can_send(user_id, group["name"])]
        everyone = settings.can_send(user_id, None)
        if groups or everyone:
            return group_picker(groups, "send", page, everyone=everyone)
        return None

    @router.message(Command("send"))
    async def start_broadcast(message: Message, state: FSMContext):
        user_id = message.from_user.id
        if settings.role(user_id) == "student":
            await message.answer("Нет доступа")
            return
        markup = picker(user_id)
        if markup is None:
            await message.answer("У вас пока нет групп для рассылки.")
            return
        await state.clear()
        await state.set_state(BroadcastFlow.group)
        await message.answer("Выйти из рассылки можно кнопкой «Отмена».", reply_markup=keyboard((CANCEL,)))
        await message.answer("Кому отправить объявление?", reply_markup=markup)

    @router.callback_query(BroadcastFlow.group, GroupChoice.filter(F.action == "send_page"))
    async def broadcast_page(query: CallbackQuery, callback_data: GroupChoice):
        markup = picker(query.from_user.id, callback_data.value)
        if markup is None:
            await query.answer("Нет доступа", show_alert=True)
            return
        await query.answer()
        await query.message.edit_reply_markup(reply_markup=markup)

    @router.callback_query(BroadcastFlow.group, GroupChoice.filter(F.action == "send"))
    async def pick_recipients(query: CallbackQuery, callback_data: GroupChoice, state: FSMContext):
        group = db.group(callback_data.value) if callback_data.value else None
        name = group["name"] if group else None
        if (callback_data.value and group is None) or not settings.can_send(query.from_user.id, name):
            await query.answer("Нет доступа", show_alert=True)
            return
        await state.update_data(group_id=group["id"] if group else None)
        await state.set_state(BroadcastFlow.text)
        await query.answer()
        await query.message.edit_text(f"Получатели: {name or 'все пользователи'}. Пришлите текст объявления.")

    @router.message(BroadcastFlow.text, F.text, ~F.text.startswith("/"))
    async def preview(message: Message, state: FSMContext):
        text = message.text.strip()
        if not text or text_length(text) > TEXT_LIMIT:
            await message.answer(f"Пришлите текст длиной до {TEXT_LIMIT} символов.")
            return
        await state.update_data(text=text)
        await state.set_state(BroadcastFlow.confirm)
        await message.answer(f"Отправить объявление?\n\n{text}", reply_markup=keyboard((SEND, CANCEL)))

    @router.message(BroadcastFlow.confirm, F.text == SEND)
    async def confirm(message: Message, state: FSMContext):
        data = await state.get_data()
        group_id = data["group_id"]
        group = db.group(group_id) if group_id is not None else None
        if (group_id is not None and group is None) or not settings.can_send(
            message.from_user.id, group["name"] if group else None,
        ):
            await state.clear()
            await message.answer("Нет доступа", reply_markup=menu())
            return
        count = broadcasts.publish(data["text"], group_id, message.from_user.id)
        await state.clear()
        await message.answer(f"Рассылка началась. Получателей: {count}.", reply_markup=menu())

    @router.message(BroadcastFlow.confirm, F.text, ~F.text.startswith("/"))
    async def ask_confirmation(message: Message):
        await message.answer("Нажмите «Отправить» или «Отмена».", reply_markup=keyboard((SEND, CANCEL)))

    @router.message(StateFilter(None), F.text == ANNOUNCEMENTS)
    async def show_announcements(message: Message):
        group = db.user_group(message.from_user.id)
        announcements = db.announcements(group["id"] if group else None)
        if not announcements:
            await message.answer("Объявлений пока нет.", reply_markup=menu())
            return
        blocks = []
        for item in announcements:
            stamp = datetime.fromisoformat(item["created_at"]).replace(tzinfo=timezone.utc)
            date_label = stamp.astimezone(settings.timezone).strftime("%d.%m.%Y")
            blocks.append(f"{date_label} · {item['name'] or 'Колледж'}\n{item['text']}")
        for part in chunks("\n\n".join(blocks)):
            await message.answer(part, reply_markup=menu())

    @router.message(Command("stats"))
    async def stats(message: Message):
        if settings.role(message.from_user.id) != "admin":
            await message.answer("Нет доступа")
            return
        total, groups = db.statistics()
        unassigned = total - sum(group["count"] for group in groups)
        lines = [f"Пользователей всего: {total}", f"Без группы: {unassigned}"]
        lines.extend(f"{group['name']}: {group['count']}" for group in groups)
        for part in chunks("\n".join(lines)):
            await message.answer(part)

    return router
