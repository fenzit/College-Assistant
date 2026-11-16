import json
import unittest
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import GetFile, SendMessage
from aiogram.types import File, Message, Update
from openpyxl import Workbook

from college_bot.backups import backup_if_due
from college_bot.bot import create_dispatcher
from college_bot.broadcasts import Broadcasts
from college_bot.config import Settings
from college_bot.database import Database
from college_bot.schedule import HEADERS, ScheduleError, read_schedule, semester_entries


def csv_bytes(*rows):
    return ("\n".join([",".join(HEADERS), *rows]) + "\n").encode()


class ScheduleTests(unittest.TestCase):
    def test_grid_expands_weeks_and_keeps_overlaps(self):
        book = Workbook()
        sheet = book.active
        sheet.append(["1 курс · недели 10–11 · 28.12 – 09.01.2027"])
        sheet.append(["День", "Пара", "СВ-21\nнед. 10"])
        sheet.append([
            "Понедельник\nДүйсенбі", "1\n08:00\n09:20",
            "Практика — практ. · Иванов · 501 нед. 10; Петров · 502 нед. 11\n\u200b\n"
            "Математика — Сидоров · 503",
        ])
        stream = BytesIO()
        book.save(stream)
        schedule = read_schedule("schedule.xlsx", stream.getvalue())
        self.assertEqual(len(schedule.warnings), 2)
        self.assertEqual(
            [(lesson.day, lesson.teacher, lesson.room) for lesson in schedule.lessons],
            [(date(2026, 12, 28), "Иванов", "501"),
             (date(2027, 1, 4), "Петров", "502"),
             (date(2026, 12, 28), "Сидоров", "503")],
        )

    def test_partial_replacement_clear_day_and_backup(self):
        with TemporaryDirectory() as folder:
            db = Database(Path(folder) / "college.sqlite3")
            db.initialize()
            content = csv_bytes(
                "2026-11-16,СВ-21,1,08:00,09:20,Математика,201,Иванов",
                "2026-11-17,СВ-21,1,08:00,09:20,Практика,202,Петров",
                "2026-11-16,СВ-22,1,08:00,09:20,Иностранный язык,203,Сидоров",
            )
            db.replace_schedule(read_schedule("schedule.csv", content))
            with self.assertRaises(ScheduleError):
                read_schedule("schedule.csv", csv_bytes(
                    "2026-11-16,СВ-21,,,,,,",
                    "2026-11-17,СВ-21,1,09:20,08:00,Практика,202,Петров",
                ))
            db.replace_schedule(read_schedule("schedule.csv", csv_bytes("2026-11-16,СВ-21,,,,,,")))
            first, last = date(2026, 11, 16), date(2026, 11, 17)
            backup = backup_if_due(db)
            self.assertIsNone(backup_if_due(db))
            snapshot = Database(backup)
            first_group, second_group = snapshot.groups()
            days = snapshot.timetable(first_group["id"], first, last)
            self.assertEqual(days[first.isoformat()].lessons, [])
            self.assertEqual(days[last.isoformat()].lessons[0]["subject"], "Практика")
            second_days = snapshot.timetable(second_group["id"], first, last)
            self.assertEqual(second_days[first.isoformat()].lessons[0]["room"], "203")

    def test_semester_grid_uses_group_blocks_and_dated_variants(self):
        book = Workbook()
        sheet = book.active
        sheet.title = "1 курс"
        sheet.append(["1 СЕМЕСТР (недели 1-3, 31.08.2026–18.09.2026)"])
        headers = ["Күні Дни", "Қоңырау уақыты Время звонка", "No ; пары", "Дәрісхана ; Аудитория"]
        sheet.append([*headers, "СВ-21", *headers, "СВ-22"])
        monday = "Дүйсенбі / Понедельник"
        sheet.append([
            monday, "08:00 09:20", 1, "501", "Практика · 501 · нед. 1 (разово) 2(разово)  Математика · 501 · нед. 3",
            monday, "08:00 09:20", "СРОП", "502", "Русский язык · 502 · нед. 2 (разово)",
        ])
        sheet.append([None] * 4 + ["Иванов · 501  Петров -501"] + [None] * 4 + ["Сидоров · 502"])
        sheet.append([None, "09:30 10:50", 2, 503, "История · 503"])
        stream = BytesIO()
        book.save(stream)
        schedule = read_schedule("schedule.xlsx", stream.getvalue())
        self.assertEqual(
            [(lesson.group, lesson.day, lesson.period, lesson.subject, lesson.teacher) for lesson in schedule.lessons],
            [("св-21", date(2026, 8, 31), 1, "Практика", "Иванов"),
             ("св-21", date(2026, 9, 7), 1, "Практика", "Иванов"),
             ("св-21", date(2026, 9, 14), 1, "Математика", "Петров"),
             ("св-22", date(2026, 9, 7), 1, "Русский язык", "Сидоров")],
        )
        self.assertEqual(schedule.warnings, ["1 курс!E5: занятие без номера недели пропущено."])

    def test_semester_import_pairs_joined_teacher_names_and_normalizes_rooms(self):
        entries, issues = semester_entries(
            "Практика · Ц2 · нед. 1    Математика · 505 · нед. 2",
            "Иванов И.И. Петров П.П. · уточнить",
            "Ц 2   505",
        )
        self.assertEqual(
            entries,
            [("Практика", "Ц2", "Иванов И.И.", {1}), ("Математика", "505", "Петров П.П.", {2})],
        )
        self.assertEqual(issues, set())
        entries, issues = semester_entries(
            "Практика · Ц 2 · нед. 1    Математика · 505 · нед. 2",
            "Иванов И.И. · Ц2, Петров П.П. · 504",
            None,
        )
        self.assertEqual(
            entries,
            [("Практика", "Ц2", "Иванов И.И.", {1}), ("Математика", "505", "Петров П.П.", {2})],
        )
        self.assertEqual(issues, {"кабинеты в строках предмета и преподавателя различаются"})

        entries, issues = semester_entries(
            "Практика · нед. 1    Математика · 505 · нед. 2",
            "Иванов И.И. · 505   Петров П.П. · уточнить",
            None,
        )
        self.assertEqual(entries[1], ("Математика", "505", "Иванов И.И.", {2}))
        self.assertIn("преподавателя для части занятий не удалось сопоставить", issues)

        entries, issues = semester_entries(
            "Практика · 505 · нед. 1    Математика · 505 · нед. 2",
            "Иванов И.И. · 505   Петров П.П.",
            None,
        )
        self.assertEqual(entries[1], ("Математика", "505", "Петров П.П.", {2}))
        self.assertEqual(issues, set())


class TelegramStub(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.rejected = set()
        self.blob = b""

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            if method.chat_id in self.rejected:
                raise TelegramForbiddenError(method=method, message="Bot was blocked")
            return Message.model_validate({
                "message_id": len(self.calls), "date": 1, "text": method.text,
                "chat": {"id": method.chat_id, "type": "private"},
            })
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="file", file_path="schedule.csv")
        return True

    async def stream_content(self, url, **kwargs):
        yield self.blob

    def messages(self, user_id):
        return [method.text for method in self.calls if isinstance(method, SendMessage) and method.chat_id == user_id]


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name)
        self.settings = Settings("123:test", 10, {20: frozenset({"св-21"}), 21: frozenset({"св-22"})},
                                 ZoneInfo("Asia/Almaty"), path)
        self.db = Database(path / "college.sqlite3")
        self.db.initialize()
        self.broadcasts = Broadcasts(self.db)
        self.dispatcher = create_dispatcher(self.settings, self.db, self.broadcasts)
        self.telegram = TelegramStub()
        self.bot = Bot(self.settings.token, session=self.telegram)
        self.counter = 0
        today = datetime.now(self.settings.timezone).date()
        self.telegram.blob = csv_bytes(
            f"{today},СВ-21,1,08:00,09:20,Математика,201,Иванов",
            f"{today},СВ-22,1,08:00,09:20,Практика,202,Петров",
        )
        self.document = {"file_id": "x", "file_unique_id": "y", "file_name": "schedule.csv"}

    async def feed(self, user_id, text=None, *, callback=None, document=None):
        self.counter += 1
        person = {"id": user_id, "is_bot": False, "first_name": "Test"}
        message = {"message_id": self.counter, "date": 1, "chat": {"id": user_id, "type": "private"}}
        payload = {"update_id": self.counter}
        if callback is not None:
            payload["callback_query"] = {
                "id": str(self.counter), "from": person, "chat_instance": "test", "message": message, "data": callback,
            }
        else:
            payload["message"] = {**message, "from": person, "text": text, "document": document}
        await self.dispatcher.feed_update(self.bot, Update.model_validate(payload))

    async def choose_group(self, user_id, group_id=1):
        await self.feed(user_id, "/start")
        await self.feed(user_id, callback=f"group:select:{group_id}")

    async def test_schedule_by_typed_date(self):
        year = datetime.now(self.settings.timezone).year
        self.telegram.blob = csv_bytes(
            f"{year}-11-12,СВ-21,1,08:00,09:20,Математика,201,Иванов",
            f"{year - 1}-11-12,СВ-21,1,08:00,09:20,Практика,202,Петров",
        )
        await self.feed(10, document=self.document)
        await self.choose_group(30)
        await self.feed(30, "Расписание")
        await self.feed(30, "12.11")
        self.assertIn("Математика, 201, Иванов", self.telegram.messages(30)[-1])
        await self.feed(30, "31.02")
        self.assertIn("Такой даты нет", self.telegram.messages(30)[-1])
        await self.feed(30, f"12.11.{year - 1}")
        self.assertIn("Практика, 202, Петров", self.telegram.messages(30)[-1])

    async def test_choose_and_change_group_by_name(self):
        await self.feed(10, document=self.document)
        await self.feed(30, "/start")
        await self.feed(30, "  св-21  ")
        group = self.db.user_group(30)
        self.assertIsNotNone(group)
        self.assertEqual(group["name"], "СВ-21")

        await self.feed(30, "Сменить группу")
        await self.feed(30, "СВ-404")
        self.assertIn("Группа не найдена", self.telegram.messages(30)[-1])
        self.assertEqual(self.db.user_group(30)["name"], "СВ-21")
        await self.feed(30, "СВ-22")
        self.assertEqual(self.db.user_group(30)["name"], "СВ-22")

    async def test_import_semester_json_and_show_draft_timetable(self):
        await self.feed(10, document=self.document)
        await self.choose_group(30)
        entry = {
            "group": "СВ-21", "day": 0, "slot": 1, "start": "08:00", "end": "09:20",
            "subject": "Информатика", "room": "201", "teacher": "Уточнить по исходному расписанию",
            "kind": "ПРАКТИКА", "weeks": [2],
        }
        data = {
            "semesterStart": "2026-08-31", "semesterEnd": "2026-09-11", "weeks": 2,
            "status": "Проект расписания", "groups": [{"id": "СВ-21"}],
            "lessons": [entry, {**entry, "day": 5, "subject": "За пределами семестра"}],
        }
        document = {**self.document, "file_name": "schedule.json"}
        self.telegram.blob = json.dumps(data).encode()
        await self.feed(30, document=document)
        self.assertEqual(self.telegram.messages(30)[-1], "Нет доступа")
        await self.feed(10, document=document)
        self.assertIn("занятий: 1", self.telegram.messages(10)[-1])

        await self.feed(30, "07.09.2026")
        response = self.telegram.messages(30)[-1]
        self.assertIn("Проект расписания", response)
        self.assertIn("[ПРАКТИКА] Информатика, 201, Уточнить по исходному расписанию", response)
        await self.feed(30, "31.08.2026")
        self.assertIn("В этот день занятий нет", self.telegram.messages(30)[-1])
        await self.feed(30, "12.09.2026")
        self.assertIn("расписание ещё не загружено", self.telegram.messages(30)[-1])

        data["lessons"] = [{**entry, "weeks": [3]}]
        self.telegram.blob = json.dumps(data).encode()
        await self.feed(10, document=document)
        self.assertIn("Расписание осталось прежним", self.telegram.messages(10)[-1])
        await self.feed(30, "07.09.2026")
        self.assertEqual(self.telegram.messages(30)[-1], response)

    async def test_import_schedule_and_group_broadcast(self):
        await self.feed(30, document=self.document)
        self.assertEqual(self.telegram.messages(30)[-1], "Нет доступа")
        await self.feed(10, document=self.document)
        self.assertEqual(len(self.db.groups()), 2)
        await self.choose_group(30)
        await self.choose_group(31, 2)
        await self.feed(30, "Расписание")
        await self.feed(30, "Сегодня")
        self.assertIn("Математика, 201, Иванов", self.telegram.messages(30)[-1])
        for command in ("/send", "/stats"):
            await self.feed(30, command)
            self.assertEqual(self.telegram.messages(30)[-1], "Нет доступа")

        await self.feed(20, "/send")
        await self.feed(20, callback="group:send:2")
        self.assertEqual(self.telegram.calls[-1].text, "Нет доступа")
        await self.feed(20, callback="group:send:1")
        await self.feed(20, "Перенесли занятие.")
        self.assertIsNone(self.db.next_announcement())
        await self.feed(20, "Отправить")
        await self.feed(20, "Отправить")
        item = self.db.next_announcement()
        await self.broadcasts.deliver(self.bot, item)
        self.assertEqual(self.telegram.messages(20)[-1], "Доставлено 1 из 1")
        self.assertIn("Перенесли занятие.", self.telegram.messages(30)[-1])
        self.assertNotIn("Перенесли занятие.", "\n".join(self.telegram.messages(31)))
        self.assertIsNone(self.db.next_announcement())
        await self.feed(30, "Объявления")
        self.assertIn("Перенесли занятие.", self.telegram.messages(30)[-1])
        await self.feed(10, "/stats")
        self.assertIn("Пользователей всего: 2", self.telegram.messages(10)[-1])

    async def test_faq_edit_and_question_reply(self):
        await self.feed(10, document=self.document)
        await self.choose_group(30)
        await self.feed(10, "/faq")
        await self.feed(10, callback="item:faq:add:0")
        await self.feed(10, "Как получить справку?")
        await self.feed(10, "Обратитесь в учебную часть.")
        await self.feed(30, "Частые вопросы")
        await self.feed(30, callback="item:faq:view:1")
        self.assertIn("Обратитесь в учебную часть.", self.telegram.messages(30)[-1])
        await self.feed(30, callback="item:faq:delete:1")
        self.assertEqual(self.telegram.calls[-1].text, "Нет доступа")

        self.telegram.rejected.add(20)
        await self.feed(30, "Задать вопрос")
        await self.feed(30, "Где проходит практика?")
        self.assertIn("Где проходит практика?", self.telegram.messages(10)[-1])
        await self.feed(21, callback="reply:1")
        self.assertEqual(self.telegram.calls[-1].text, "Нет доступа")
        await self.feed(10, callback="reply:1")
        await self.feed(10, "В мастерской.")
        self.assertEqual(self.db.question(1)["status"], "answered")
        self.assertIn("В мастерской.", self.telegram.messages(30)[-1])


if __name__ == "__main__":
    unittest.main()
