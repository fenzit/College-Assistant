from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import Settings
from .database import Database, Directory
from .ui import CANCEL, CONTACTS, FAQ, TEXT_LIMIT, chunks, keyboard, menu, text_length

PAGE_SIZE = 10
DELETE = "Удалить"
DIRECTORY_NAMES = {"contacts": CONTACTS, "faq": FAQ}


class DirectoryAction(CallbackData, prefix="item"):
    kind: Directory
    action: str
    value: int


class EditFlow(StatesGroup):
    title = State()
    body = State()
    delete = State()


def item_list(items, kind: Directory, page: int = 0, *, admin: bool = False):
    last_page = max(0, (len(items) - 1) // PAGE_SIZE)
    page = min(max(page, 0), last_page)
    builder = InlineKeyboardBuilder()
    if admin:
        builder.button(text="Добавить", callback_data=DirectoryAction(kind=kind, action="add", value=0))
    for item in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        action = "manage_item" if admin else "view"
        builder.button(text=item["title"], callback_data=DirectoryAction(kind=kind, action=action, value=item["id"]))
    builder.adjust(1)
    page_action = "manage_page" if admin else "page"
    for label, target in (("‹ Предыдущая", page - 1), ("Следующая ›", page + 1)):
        if 0 <= target <= last_page:
            builder.button(text=label, callback_data=DirectoryAction(kind=kind, action=page_action, value=target))
    return builder.as_markup()


def directory_router(settings: Settings, db: Database) -> Router:
    router = Router()
    router.message.filter(F.chat.type == ChatType.PRIVATE, F.from_user)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    @router.message(StateFilter(None), F.text == CONTACTS)
    async def contacts(message: Message):
        items = db.directory("contacts")
        text = "\n\n".join(f"{item['title']}\n{item['body']}" for item in items)
        for part in chunks(text or "Контакты пока не добавлены."):
            await message.answer(part, reply_markup=menu())

    @router.message(StateFilter(None), F.text == FAQ)
    async def faq(message: Message):
        items = db.directory("faq")
        if not items:
            await message.answer("Частые вопросы пока не добавлены.", reply_markup=menu())
            return
        await message.answer("Выберите вопрос.", reply_markup=item_list(items, "faq"))

    @router.callback_query(DirectoryAction.filter(F.action == "page"))
    async def faq_page(query: CallbackQuery, callback_data: DirectoryAction):
        if callback_data.kind != "faq":
            await query.answer("Список недоступен.", show_alert=True)
            return
        await query.answer()
        await query.message.edit_reply_markup(
            reply_markup=item_list(db.directory("faq"), "faq", callback_data.value)
        )

    @router.callback_query(DirectoryAction.filter(F.action == "view"))
    async def answer_faq(query: CallbackQuery, callback_data: DirectoryAction):
        item = db.directory_item(callback_data.kind, callback_data.value)
        if item is None:
            await query.answer("Запись удалена.", show_alert=True)
            return
        await query.answer()
        await query.message.answer(f"{item['title']}\n\n{item['body']}")

    @router.message(Command("admin"))
    async def admin_help(message: Message, state: FSMContext):
        if settings.role(message.from_user.id) != "admin":
            await message.answer("Нет доступа")
            return
        await state.clear()
        await message.answer(
            "/import — загрузить расписание\n"
            "/contacts — редактировать контакты\n"
            "/faq — редактировать частые вопросы\n"
            "/send — сделать рассылку\n"
            "/stats — посмотреть число пользователей",
            reply_markup=menu(),
        )

    @router.message(Command("contacts", "faq"))
    async def manage_directory(message: Message, state: FSMContext, command):
        if settings.role(message.from_user.id) != "admin":
            await message.answer("Нет доступа")
            return
        await state.clear()
        kind = command.command
        await message.answer(DIRECTORY_NAMES[kind], reply_markup=menu())
        await message.answer("Выберите запись или добавьте новую.", reply_markup=item_list(db.directory(kind), kind, admin=True))

    @router.callback_query(DirectoryAction.filter(F.action.in_({"manage_page", "manage_item", "add", "edit", "delete"})))
    async def manage_item(query: CallbackQuery, callback_data: DirectoryAction, state: FSMContext):
        if settings.role(query.from_user.id) != "admin":
            await query.answer("Нет доступа", show_alert=True)
            return
        kind, action, value = callback_data.kind, callback_data.action, callback_data.value
        if action == "manage_page":
            await query.answer()
            await query.message.edit_reply_markup(reply_markup=item_list(db.directory(kind), kind, value, admin=True))
            return
        item = db.directory_item(kind, value)
        if item is None and action != "add":
            await query.answer("Запись удалена.", show_alert=True)
            return

        await query.answer()
        if action == "manage_item":
            builder = InlineKeyboardBuilder()
            builder.button(text="Изменить", callback_data=DirectoryAction(kind=kind, action="edit", value=value))
            builder.button(text=DELETE, callback_data=DirectoryAction(kind=kind, action="delete", value=value))
            await query.message.answer(f"{item['title']}\n\n{item['body']}", reply_markup=builder.as_markup())
            return

        await state.clear()
        await state.update_data(kind=kind, item_id=item["id"] if item else None)
        if action == "delete":
            await state.set_state(EditFlow.delete)
            await query.message.answer(f"Удалить «{item['title']}»?", reply_markup=keyboard((DELETE, CANCEL)))
            return
        await state.set_state(EditFlow.title)
        prompt = "Напишите вопрос." if kind == "faq" else "Напишите название контакта."
        await query.message.answer(prompt, reply_markup=keyboard((CANCEL,)))

    @router.message(EditFlow.title, F.text, ~F.text.startswith("/"))
    async def enter_title(message: Message, state: FSMContext):
        if not await check_editor(message, state):
            return
        title = " ".join(message.text.split())
        if not 1 <= text_length(title) <= 120:
            await message.answer("Допустимая длина заголовка от 1 до 120 символов.")
            return
        data = await state.update_data(title=title)
        await state.set_state(EditFlow.body)
        prompt = "Напишите ответ." if data["kind"] == "faq" else "Введите телефон, кабинет или другие данные."
        await message.answer(prompt)

    @router.message(EditFlow.body, F.text, ~F.text.startswith("/"))
    async def save(message: Message, state: FSMContext):
        if not await check_editor(message, state):
            return
        body = message.text.strip()
        if not 1 <= text_length(body) <= TEXT_LIMIT:
            await message.answer(f"Допустимая длина текста от 1 до {TEXT_LIMIT} символов.")
            return
        data = await state.get_data()
        saved = db.save_directory_item(data["kind"], data["item_id"], data["title"], body)
        await state.clear()
        await message.answer("Запись сохранена." if saved else "Запись уже удалена.", reply_markup=menu())

    @router.message(EditFlow.delete, F.text == DELETE)
    async def delete(message: Message, state: FSMContext):
        if not await check_editor(message, state):
            return
        data = await state.get_data()
        deleted = db.delete_directory_item(data["kind"], data["item_id"])
        await state.clear()
        await message.answer("Запись удалена." if deleted else "Запись уже удалена.", reply_markup=menu())

    async def check_editor(message: Message, state: FSMContext) -> bool:
        if settings.role(message.from_user.id) == "admin":
            return True
        await state.clear()
        await message.answer("Нет доступа", reply_markup=menu())
        return False

    return router
