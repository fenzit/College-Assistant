from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

PAGE_SIZE = 12
CHANGE_GROUP = "Сменить группу"
BACK = "Назад"
CANCEL = "Отмена"
SCHEDULE = "Расписание"
TODAY, TOMORROW, WEEK = "Сегодня", "Завтра", "Неделя"


class GroupChoice(CallbackData, prefix="group"):
    action: str
    value: int


def menu() -> ReplyKeyboardMarkup:
    return keyboard((SCHEDULE,), (CHANGE_GROUP,))


def keyboard(*rows: tuple[str, ...]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in rows],
        resize_keyboard=True,
    )


def group_picker(groups, action: str, page: int = 0, *, everyone: bool = False) -> InlineKeyboardMarkup:
    last_page = max(0, (len(groups) - 1) // PAGE_SIZE)
    page = min(max(page, 0), last_page)
    builder = InlineKeyboardBuilder()
    for group in groups[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        builder.button(text=group["name"], callback_data=GroupChoice(action=action, value=group["id"]))
    builder.adjust(2)
    if everyone:
        builder.row(InlineKeyboardButton(
            text="Все пользователи", callback_data=GroupChoice(action=action, value=0).pack(),
        ))
    pages = [
        InlineKeyboardButton(text=label, callback_data=GroupChoice(action=f"{action}_page", value=target).pack())
        for label, target in (("‹", page - 1), ("›", page + 1))
        if 0 <= target <= last_page
    ]
    if pages:
        builder.row(*pages)
    return builder.as_markup()


def chunks(text: str, limit: int = 3800):
    while len(text.encode("utf-16-le")) // 2 > limit:
        end = limit
        while len(text[:end].encode("utf-16-le")) // 2 > limit:
            end -= 1
        split = text.rfind("\n", 0, end + 1)
        if split < end // 2:
            split = end
        yield text[:split].rstrip()
        text = text[split:].lstrip("\n")
    if text:
        yield text
