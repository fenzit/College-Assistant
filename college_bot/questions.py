from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import Settings
from .database import Database
from .telegram import send_text
from .ui import ASK, CANCEL, TEXT_LIMIT, keyboard, menu, text_length


class QuestionReply(CallbackData, prefix="reply"):
    question_id: int


class QuestionFlow(StatesGroup):
    ask = State()
    reply = State()


def question_router(settings: Settings, db: Database, show_groups) -> Router:
    router = Router()
    router.message.filter(F.chat.type == ChatType.PRIVATE, F.from_user)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    def can_answer(user_id: int, question) -> bool:
        return settings.role(user_id) == "admin" or (
            settings.role(user_id) == "teacher" and question["assignee_id"] == user_id
        )

    @router.message(StateFilter(None), F.text == ASK)
    async def ask(message: Message, state: FSMContext):
        group = db.user_group(message.from_user.id)
        if group is None:
            await show_groups(message, state, "Сначала выберите группу.")
            return
        await state.set_state(QuestionFlow.ask)
        await message.answer("Напишите вопрос преподавателю.", reply_markup=keyboard((CANCEL,)))

    @router.message(QuestionFlow.ask, F.text, ~F.text.startswith("/"))
    async def forward_question(message: Message, state: FSMContext, bot: Bot):
        text = message.text.strip()
        if not 1 <= text_length(text) <= TEXT_LIMIT:
            await message.answer(f"Напишите вопрос длиной до {TEXT_LIMIT} символов.")
            return
        user_id = message.from_user.id
        group = db.user_group(user_id)
        if group is None:
            await state.clear()
            await show_groups(message, state, "Сначала выберите группу.")
            return
        recipients = settings.question_recipients(group["name"])
        question_id = db.create_question(user_id, recipients[0], text)
        await state.clear()
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Ответить", callback_data=QuestionReply(question_id=question_id).pack()),
        ]])
        question = f"Вопрос от группы {group['name']}\n\n{text}"
        for recipient in recipients:
            db.assign_question(question_id, recipient)
            status = await send_text(bot, recipient, question, reply_markup=markup)
            if status == "sent":
                await message.answer("Вопрос отправлен. Ответ придёт сюда.", reply_markup=menu())
                return
            if status == "unknown":
                await message.answer("Telegram не подтвердил отправку вопроса.", reply_markup=menu())
                return
        db.discard_question(question_id)
        await message.answer("Вопрос не доставлен. Обратитесь к администратору колледжа.", reply_markup=menu())

    @router.callback_query(QuestionReply.filter())
    async def prepare_reply(query: CallbackQuery, callback_data: QuestionReply, state: FSMContext):
        question = db.question(callback_data.question_id)
        if question is None:
            await query.answer("Вопрос недоступен.", show_alert=True)
            return
        if not can_answer(query.from_user.id, question):
            await query.answer("Нет доступа", show_alert=True)
            return
        if question["status"] != "pending":
            await query.answer("По этому вопросу уже отправляли ответ.", show_alert=True)
            return
        await state.clear()
        await state.update_data(question_id=question["id"])
        await state.set_state(QuestionFlow.reply)
        await query.answer()
        await query.message.answer(
            f"Напишите ответ на вопрос.\n\n{question['text']}", reply_markup=keyboard((CANCEL,)),
        )

    @router.message(QuestionFlow.reply, F.text, ~F.text.startswith("/"))
    async def reply(message: Message, state: FSMContext, bot: Bot):
        text = message.text.strip()
        if not 1 <= text_length(text) <= TEXT_LIMIT:
            await message.answer(f"Напишите ответ длиной до {TEXT_LIMIT} символов.")
            return
        question_id = (await state.get_data())["question_id"]
        question = db.question(question_id)
        await state.clear()
        if question is None or not can_answer(message.from_user.id, question):
            await message.answer("Нет доступа", reply_markup=menu())
            return
        if not db.claim_reply(question_id):
            await message.answer("По этому вопросу уже отправляли ответ.", reply_markup=menu())
            return
        status = "unknown"
        try:
            status = await send_text(bot, question["student_id"], f"Ответ на ваш вопрос\n\n{text}")
        finally:
            db.finish_reply(question_id, {"sent": "answered", "failed": "pending", "unknown": "unknown"}[status])
        confirmation = {
            "sent": "Ответ отправлен.",
            "failed": "Ответ не доставлен. Можно повторить отправку.",
            "unknown": "Telegram не подтвердил отправку ответа.",
        }
        await message.answer(confirmation[status], reply_markup=menu())

    return router
