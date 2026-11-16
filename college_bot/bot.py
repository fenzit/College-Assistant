from aiogram import Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import CallbackQuery, Message

from .broadcasts import Broadcasts, broadcast_router
from .config import Settings
from .database import Database
from .directory import directory_router
from .timetable import schedule_router
from .ui import BACK, CANCEL, CHANGE_GROUP, GroupChoice, group_picker, menu


def create_dispatcher(settings: Settings, db: Database, broadcasts: Broadcasts) -> Dispatcher:
    dp = Dispatcher(events_isolation=SimpleEventIsolation(), settings=settings, db=db)
    router = Router()
    router.message.filter(F.chat.type == ChatType.PRIVATE, F.from_user)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        user = message.from_user
        db.register(user.id, user.full_name, settings.role(user.id))
        group = db.user_group(user.id)
        if group is None:
            await show_groups(message, "Здравствуйте! Выберите вашу группу.")
            return
        await message.answer(f"Ваша группа: {group['name']}.", reply_markup=menu())

    async def show_groups(message: Message, prompt: str):
        groups = db.groups()
        if groups:
            await message.answer(prompt, reply_markup=group_picker(groups, "select"))
        else:
            await message.answer("Список групп пока пуст. Обратитесь к администратору.", reply_markup=menu())

    @router.message(F.text == CHANGE_GROUP)
    async def change_group(message: Message, state: FSMContext):
        await state.clear()
        await show_groups(message, "Выберите группу.")

    @router.callback_query(GroupChoice.filter(F.action == "select_page"))
    async def group_page(query: CallbackQuery, callback_data: GroupChoice):
        await query.answer()
        await query.message.edit_reply_markup(
            reply_markup=group_picker(db.groups(), "select", callback_data.value)
        )

    @router.callback_query(GroupChoice.filter(F.action == "select"))
    async def select_group(query: CallbackQuery, callback_data: GroupChoice, state: FSMContext):
        group = db.group(callback_data.value)
        if group is None:
            await query.answer("Группа не найдена.", show_alert=True)
            return
        user = query.from_user
        db.register(user.id, user.full_name, settings.role(user.id))
        db.set_group(user.id, group["id"])
        await state.clear()
        await query.answer()
        await query.message.edit_text(f"Выбрана группа {group['name']}.")
        await query.message.answer("Главное меню", reply_markup=menu())

    @router.message(Command("cancel"))
    @router.message(F.text.in_({BACK, CANCEL}))
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("Главное меню", reply_markup=menu())

    dp.include_router(router)
    dp.include_router(schedule_router(settings, db, show_groups))
    dp.include_router(broadcast_router(settings, db, broadcasts))
    dp.include_router(directory_router(settings, db))
    return dp
