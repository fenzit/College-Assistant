import asyncio
import logging
from datetime import date, datetime, timedelta
from io import BytesIO
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from openpyxl.utils.exceptions import InvalidFileException

from .config import Settings
from .database import Database
from .schedule import ScheduleError, WEEKDAYS, read_schedule
from .ui import BACK, SCHEDULE, TODAY, TOMORROW, WEEK, chunks, keyboard

logger = logging.getLogger(__name__)
DATE_PATTERN = r"^[0-9]{1,2}\.[0-9]{1,2}(?:\.[0-9]{4})?$"


def schedule_router(settings: Settings, db: Database, show_groups) -> Router:
    router = Router()
    router.message.filter(F.chat.type == ChatType.PRIVATE, F.from_user)

    @router.message(StateFilter(None), F.text == SCHEDULE)
    async def show_schedule_menu(message: Message, state: FSMContext):
        if db.user_group(message.from_user.id) is None:
            await show_groups(message, state, "Сначала выберите группу.")
            return
        year = datetime.now(settings.timezone).year
        await message.answer(
            f"На какой день показать расписание? Можно ввести дату, например 12.11 или 12.11.{year}.",
            reply_markup=keyboard((TODAY, TOMORROW, WEEK), (BACK,)),
        )

    @router.message(StateFilter(None), F.text.in_({TODAY, TOMORROW, WEEK}) | F.text.strip().regexp(DATE_PATTERN))
    async def show_schedule(message: Message, state: FSMContext):
        group = db.user_group(message.from_user.id)
        if group is None:
            await show_groups(message, state, "Сначала выберите группу.")
            return
        today = datetime.now(settings.timezone).date()
        first = last = today
        if message.text == TOMORROW:
            first = last = today + timedelta(days=1)
        elif message.text == WEEK:
            first = today - timedelta(days=today.weekday())
            last = first + timedelta(days=6)
        elif message.text != TODAY:
            day, month, *years = map(int, message.text.strip().split("."))
            try:
                first = last = date(years[0] if years else today.year, month, day)
            except ValueError:
                await message.answer("Такой даты нет. Проверьте день, месяц и год.")
                return
        days = db.timetable(group["id"], first, last)
        text = format_timetable(group["name"], days, first, last)
        for part in chunks(text):
            await message.answer(part)

    @router.message(Command("import"))
    async def ask_file(message: Message, state: FSMContext):
        if settings.role(message.from_user.id) != "admin":
            await message.answer("Нет доступа")
            return
        await state.clear()
        await message.answer("Пришлите расписание файлом .xlsx или .csv.")

    @router.message(F.document.file_name.lower().endswith((".xlsx", ".csv")))
    async def upload_schedule(message: Message, bot: Bot):
        if settings.role(message.from_user.id) != "admin":
            await message.answer("Нет доступа")
            return
        if (message.document.file_size or 0) > 20 * 1024 * 1024:
            await message.answer("Файл больше 20 МБ. Разбейте расписание на несколько файлов.")
            return

        await message.answer("Проверяю расписание.")
        with BytesIO() as stream:
            await bot.download(message.document, destination=stream)
            try:
                schedule = await asyncio.to_thread(read_schedule, message.document.file_name, stream.getvalue())
            except ScheduleError as exc:
                for part in chunks(f"Расписание осталось прежним.\n{exc}"):
                    await message.answer(part)
                return
            except (BadZipFile, InvalidFileException, ParseError, ValueError, KeyError, IndexError, OSError):
                logger.exception("Не удалось прочитать файл расписания")
                await message.answer("Файл не удалось прочитать. Расписание осталось прежним.")
                return
        await asyncio.to_thread(db.replace_schedule, schedule)
        text = f"Расписание обновлено. Групп: {len(schedule.groups)}, занятий: {len(schedule.lessons)}."
        if schedule.warnings:
            text += "\n\nПроверьте эти ячейки:\n" + "\n".join(schedule.warnings)
        for part in chunks(text):
            await message.answer(part)

    return router


def format_timetable(name, days, first, last) -> str:
    sections = [name]
    for offset in range((last - first).days + 1):
        day = first + timedelta(days=offset)
        heading = f"{WEEKDAYS[day.weekday()]}, {day:%d.%m}"
        lessons = days.get(day.isoformat())
        if lessons is None:
            lines = ["На этот день расписание ещё не загружено."]
        elif not lessons:
            lines = ["В этот день занятий нет."]
        else:
            lines = []
            for lesson in lessons:
                room = lesson["room"] or "кабинет не указан"
                teacher = lesson["teacher"] or "преподаватель не указан"
                lines.append(
                    f"{lesson['period']} пара, {lesson['starts_at']}–{lesson['ends_at']} "
                    f"— {lesson['subject']}, {room}, {teacher}"
                )
        sections.append("\n".join([heading, *lines]))
    return "\n\n".join(sections)
