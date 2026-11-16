import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from io import BytesIO, StringIO

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from .config import group_key

HEADERS = ("date", "group", "period", "starts_at", "ends_at", "subject", "room", "teacher")
WEEKDAYS = (
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье",
)
WEEK_LIST = re.compile(r"\s+нед\.\s*(\d+(?:\s*[-–—,]\s*\d+)*)$", re.IGNORECASE)
GRID_TITLE = re.compile(
    r"недели\s+(\d+)\s*[-–—]\s*(\d+)\s*·\s*"
    r"(\d{1,2}\.\d{1,2}(?:\.\d{4})?)\s*[-–—]\s*(\d{1,2}\.\d{1,2}\.\d{4})",
    re.IGNORECASE,
)


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Lesson:
    group: str
    day: date
    period: int
    starts_at: str
    ends_at: str
    subject: str
    room: str
    teacher: str


@dataclass
class Schedule:
    groups: dict[str, str] = field(default_factory=dict)
    coverage: set[tuple[str, date]] = field(default_factory=set)
    lessons: list[Lesson] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_group(self, name: str) -> str:
        key = group_key(name)
        self.groups.setdefault(key, name)
        return key


def read_schedule(filename: str, content: bytes) -> Schedule:
    suffix = filename.lower().rsplit(".", 1)[-1]
    result = Schedule()
    if suffix == "csv":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("cp1251", errors="replace")
        first_line = text.splitlines()[0] if text.splitlines() else ""
        delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
        read_table(csv.reader(StringIO(text, newline=""), delimiter=delimiter), result, "CSV")
    elif suffix == "xlsx":
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
        try:
            for sheet in workbook.worksheets:
                if sheet.sheet_state != "visible":
                    continue
                rows = iter(sheet.values)
                for index, row in enumerate(rows, start=1):
                    if any(value is not None for value in row):
                        break
                else:
                    continue
                if clean(row[0]).casefold() == HEADERS[0]:
                    read_table(rows, result, sheet.title, headers=row, row_number=index)
                else:
                    read_grid(row, rows, index, result, sheet.title)
        finally:
            workbook.close()
    else:
        raise ScheduleError("Поддерживаются файлы .xlsx и .csv.")
    if not result.coverage:
        raise ScheduleError("В файле нет расписания.")
    return result


def clean(value) -> str:
    return "" if value is None else str(value).replace("\u200b", "").strip()


def as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = clean(value)
    if "." in text:
        return datetime.strptime(text, "%d.%m.%Y").date()
    return date.fromisoformat(text)


def time_label(value) -> str:
    if isinstance(value, datetime):
        value = value.time()
    if not isinstance(value, time):
        value = time.fromisoformat(clean(value))
    if value.second or value.microsecond or value.tzinfo is not None:
        raise ValueError
    return value.strftime("%H:%M")


def period_and_time(period, starts_at, ends_at) -> tuple[int, str, str]:
    number = int(period)
    start = time_label(starts_at)
    end = time_label(ends_at)
    if not 1 <= number <= 99 or float(period) != number or start >= end:
        raise ValueError
    return number, start, end


def read_table(rows, result: Schedule, source: str, *, headers=None, row_number: int = 1):
    rows = iter(rows)
    if headers is None:
        headers = next(rows, ())
    fields = tuple(clean(value).casefold() for value in headers)
    if fields[:len(HEADERS)] != HEADERS or any(fields[len(HEADERS):]):
        raise ScheduleError(f"{source}: ожидаются столбцы {', '.join(HEADERS)}.")
    for number, row in enumerate(rows, start=row_number + 1):
        if not any(clean(value) for value in row):
            continue
        row = (*row, *(None,) * max(0, len(HEADERS) - len(row)))
        raw_day, raw_group, period, starts_at, ends_at, subject, room, teacher, *extra = row
        try:
            day = as_date(raw_day)
            name = clean(raw_group)
            if not name or any(clean(value) for value in extra):
                raise ValueError
            key = result.add_group(name)
            result.coverage.add((key, day))
            if not any(clean(value) for value in (period, starts_at, ends_at, subject, room, teacher)):
                continue
            number_in_day, start, end = period_and_time(period, starts_at, ends_at)
            if not clean(subject):
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise ScheduleError(f"{source}, строка {number}: проверьте дату, группу, номер пары, время и предмет.") from None
        result.lessons.append(Lesson(key, day, number_in_day, start, end, clean(subject), clean(room), clean(teacher)))


def week_numbers(value: str) -> set[int]:
    weeks = set()
    for entry in value.split(","):
        limits = [int(number) for number in re.split(r"[-–—]", entry)]
        first, last = limits[0], limits[-1]
        if not 1 <= first <= last <= 53:
            raise ValueError
        weeks.update(range(first, last + 1))
    return weeks


def split_weeks(value: str) -> tuple[str, set[int] | None]:
    match = WEEK_LIST.search(value)
    if match is None:
        if re.search(r"\bнед\.", value, re.IGNORECASE):
            raise ValueError
        return value, None
    return value[:match.start()].strip(), week_numbers(match[1])


def grid_dates(title: re.Match) -> tuple[dict[int, date], date]:
    first_week, last_week = int(title[1]), int(title[2])
    end = as_date(title[4])
    start_text = title[3]
    if start_text.count(".") == 1:
        day, month = map(int, start_text.split("."))
        year = end.year - int((month, day) > (end.month, end.day))
        start = date(year, month, day)
    else:
        start = as_date(start_text)
    if not 1 <= first_week <= last_week <= 53 or start.weekday() != 0:
        raise ValueError
    if (end - start).days // 7 != last_week - first_week:
        raise ValueError
    return {week: start + timedelta(weeks=week - first_week) for week in range(first_week, last_week + 1)}, end


def read_grid(first_row, rows, first_number: int, result: Schedule, source: str):
    title = None
    for row_number, row in numbered_rows(first_row, rows, first_number):
        if not row:
            continue
        title = title or GRID_TITLE.search(clean(row[0]))
        if len(row) > 1 and clean(row[1]).casefold() == "пара":
            headers = row
            break
    else:
        raise ScheduleError(f"{source}: не найдена строка с заголовком «Пара».")
    if title is None:
        raise ScheduleError(f"{source}: укажите номера учебных недель и даты в заголовке.")
    try:
        mondays, end = grid_dates(title)
    except ValueError:
        raise ScheduleError(f"{source}: даты и номера учебных недель в заголовке расходятся.") from None

    groups = {}
    for index, header in enumerate(headers[2:], start=2):
        if not clean(header):
            continue
        try:
            name, active_weeks = split_weeks(" ".join(clean(header).split()))
            if not name or (active_weeks and not active_weeks <= mondays.keys()):
                raise ValueError
        except ValueError:
            raise ScheduleError(f"{source}!{get_column_letter(index + 1)}{row_number}: проверьте группу и недели.") from None
        key = result.add_group(name)
        groups[index] = key, active_weeks or set(mondays)
        start = next(iter(mondays.values()))
        result.coverage.update((key, start + timedelta(days=offset)) for offset in range((end - start).days + 1))
    if not groups:
        raise ScheduleError(f"{source}: нет заголовков групп.")

    weekday = None
    for row_number, row in enumerate(rows, start=row_number + 1):
        if not any(clean(value) for value in row[1:]):
            continue
        location = f"{source}!B{row_number}"
        try:
            if clean(row[0]):
                weekday = WEEKDAYS.index(clean(row[0]).splitlines()[0])
            if weekday is None:
                raise ValueError
            period, start, finish = period_and_time(*clean(row[1]).split())
        except (ValueError, TypeError):
            raise ScheduleError(f"{location}: проверьте день недели, номер пары и время.") from None
        for index, value in enumerate(row[2:], start=2):
            if not clean(value):
                continue
            location = f"{source}!{get_column_letter(index + 1)}{row_number}"
            if index not in groups:
                raise ScheduleError(f"{location}: у столбца нет названия группы.")
            key, active_weeks = groups[index]
            read_grid_cell(
                clean(value), result, location, key, active_weeks, mondays, end,
                weekday, period, start, finish,
            )


def numbered_rows(first_row, rows, first_number):
    yield first_number, first_row
    yield from enumerate(rows, start=first_number + 1)


def read_grid_cell(
    value, result, location, group, active_weeks, mondays, end, weekday, period, start, finish,
):
    counts = Counter()
    outside_group = set()
    try:
        for line in value.splitlines():
            if not line.strip():
                continue
            subject, entries = line.strip().split(" — ", 1)
            parts = [part.strip() for part in entries.split(" · ", 1)]
            if parts[0].casefold() in {"сроп", "практ.", "лек.", "ментор"}:
                subject = f"{subject} ({parts[0]})"
                parts = parts[1].split(" · ", 1)
            teacher, rooms = parts
            for variant in rooms.split(";"):
                variant = variant.strip()
                if " · " in variant:
                    teacher, variant = variant.split(" · ", 1)
                room, weeks = split_weeks(variant)
                if not room.strip() or not teacher.strip():
                    raise ValueError
                for week in sorted(weeks or active_weeks):
                    if week not in mondays or (day := mondays[week] + timedelta(days=weekday)) > end:
                        raise ValueError
                    outside_group.update({week} - active_weeks)
                    counts[week] += 1
                    result.lessons.append(Lesson(group, day, period, start, finish, subject, room, teacher.strip()))
    except (ValueError, IndexError):
        raise ScheduleError(f"{location}: не удалось разобрать занятие, кабинет или недели.") from None
    if outside_group:
        result.warnings.append(f"{location}: занятия вне недель группы ({format_weeks(outside_group)}).")
    overlaps = {week for week, count in counts.items() if count > 1}
    if overlaps:
        result.warnings.append(f"{location}: несколько занятий на одну пару ({format_weeks(overlaps)}).")


def format_weeks(weeks) -> str:
    return ", ".join(map(str, sorted(weeks)))
