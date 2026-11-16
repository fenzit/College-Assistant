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
    r"недели\s+(\d+)\s*[-–—]\s*(\d+)\s*[·,]\s*"
    r"(\d{1,2}\.\d{1,2}(?:\.\d{4})?)\s*[-–—]\s*(\d{1,2}\.\d{1,2}\.\d{4})",
    re.IGNORECASE,
)
SEMESTER_HEADERS = ("Күні Дни", "Қоңырау уақыты Время звонка", "No ; пары", "Дәрісхана ; Аудитория")
WEEK_TERM = r"[0-9]{1,2}(?![0-9])(?:\s*[-–—]\s*[0-9]{1,2}(?![0-9]))?"
WEEK_NOTE = r"(?:\s*,?\s*\((?:разово|разоао|пазово|весь семестр)\)?)?"
SEMESTER_WEEKS = re.compile(
    rf"(?:\bнед[.), \t]*|(?<=\s)-(?=[0-9]{{1,2}}\s*[-–—]))"
    rf"(?P<weeks>{WEEK_TERM}{WEEK_NOTE}(?:(?:\s*,\s*|(?<=\))\s*){WEEK_TERM}{WEEK_NOTE})*)",
    re.IGNORECASE,
)
ROOM_LABEL = r"(?:[0-9]{3,4}[а-я]?|Ц[ \t]*\d+|спортзал(?: \(поток \d+\))?|уточнить|уточняется)"
ROOM_SUFFIX = re.compile(rf"(?:^|[\s·,:-])(?P<room>{ROOM_LABEL})$", re.IGNORECASE)
ROOM_CORRECTION = re.compile(rf"\s*-\s*(?P<room>{ROOM_LABEL})(?=\s|$)", re.IGNORECASE)
TEACHER_SEPARATOR = re.compile(r"[ \t]{2,}(?![^()]*\))")
TEACHER_ROOM_SEPARATOR = re.compile(
    rf"(?<!\w)(?P<room>{ROOM_LABEL})[ \t]*,[ \t]*(?=[^\W\d_])(?![^()]*\))", re.IGNORECASE,
)
JOINED_TEACHERS = re.compile(r"(?<=[А-ЯЁӘІҢҒҮҰҚӨҺ]\.)(?:[ \t]+-?|-[ \t]*)(?=[^\W\d_]+[ \t]+[^\W\d_]\.)")
UNKNOWN_ROOMS = {"", "уточнить", "уточняется"}


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
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True, keep_links=False)
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
        if tuple(clean(value) for value in row[:4]) == SEMESTER_HEADERS:
            read_layout = read_semester_grid
            headers = row
            break
        if len(row) > 1 and clean(row[1]).casefold() == "пара":
            read_layout = read_compact_grid
            headers = row
            break
    else:
        raise ScheduleError(f"{source}: не найдена строка с заголовками расписания.")
    if title is None:
        raise ScheduleError(f"{source}: укажите номера учебных недель и даты в заголовке.")
    try:
        mondays, end = grid_dates(title)
    except ValueError:
        raise ScheduleError(f"{source}: даты и номера учебных недель в заголовке расходятся.") from None
    read_layout(headers, rows, row_number, result, source, mondays, end)


def add_grid_group(result, name, mondays, end):
    key = result.add_group(name)
    start = next(iter(mondays.values()))
    result.coverage.update((key, start + timedelta(days=offset)) for offset in range((end - start).days + 1))
    return key


def read_compact_grid(headers, rows, row_number, result, source, mondays, end):
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
        key = add_grid_group(result, name, mondays, end)
        groups[index] = key, active_weeks or set(mondays)
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


def read_semester_grid(headers, rows, row_number, result, source, mondays, end):
    groups = {}
    for offset in range(0, len(headers), 5):
        block = tuple(clean(value) for value in headers[offset:offset + 5])
        if not any(block):
            continue
        if len(block) != 5 or block[:4] != SEMESTER_HEADERS or not block[4]:
            raise ScheduleError(f"{source}!{get_column_letter(offset + 1)}{row_number}: проверьте заголовки группы.")
        groups[offset] = add_grid_group(result, block[4], mondays, end)

    weekdays = {}
    numbered = enumerate(rows, start=row_number + 1)
    for number, upper in numbered:
        _, lower = next(numbered, (number + 1, ()))
        read_semester_period(upper, lower, number, result, source, groups, weekdays, mondays, end)


def row_value(row, index):
    return row[index] if index < len(row) else None


def read_semester_period(upper, lower, number, result, source, groups, weekdays, mondays, end):
    for offset, group in groups.items():
        weekday = clean(row_value(upper, offset))
        lesson_text = row_value(upper, offset + 4)
        location = f"{source}!{get_column_letter(offset + 5)}{number}"
        try:
            if weekday:
                weekdays[offset] = WEEKDAYS.index(weekday.rsplit("/", 1)[-1].strip())
            if not clean(lesson_text):
                continue
            times = clean(row_value(upper, offset + 1)).split()[:2]
            label = row_value(upper, offset + 2)
            if clean(label).casefold() == "сроп":
                numbers = {
                    row_value(upper, other + 2) for other in groups
                    if clean(row_value(upper, other + 1)).split()[:2] == times
                    and isinstance(row_value(upper, other + 2), (int, float))
                }
                label = numbers.pop() if len(numbers) == 1 else None
            period, start, finish = period_and_time(label, *times)
            entries, issues = semester_entries(lesson_text, row_value(lower, offset + 4), row_value(upper, offset + 3))
            counts = Counter()
            for subject, room, teacher, weeks in entries:
                for week in sorted(weeks):
                    day = mondays[week] + timedelta(days=weekdays[offset])
                    if day > end:
                        issues.add("пары за последней датой семестра пропущены")
                        continue
                    result.lessons.append(Lesson(group, day, period, start, finish, subject, room, teacher))
                    counts[week] += 1
            overlaps = {week for week, count in counts.items() if count > 1}
            if overlaps:
                issues.add(f"несколько занятий на одну пару ({format_weeks(overlaps)})")
            if issues:
                result.warnings.append(f"{location}: {'. '.join(sorted(issues))}.")
        except (KeyError, TypeError, ValueError):
            raise ScheduleError(f"{location}: проверьте расписание пары, время и номера недель.") from None


def semester_entries(lesson_text, teacher_text, room_text):
    subject_rows = [list(semester_subjects(line)) for line in str(lesson_text).split("\n")]
    teacher_rows = [semester_teachers(line) for line in str(teacher_text or "").split("\n")]
    room_rows = str(room_text or "").split("\n")
    aligned = len(subject_rows) == len(teacher_rows) and all(
        len(subjects) <= 1 and len(teachers) == 1 for subjects, teachers in zip(subject_rows, teacher_rows)
    )
    # Several variants can share a physical line after manual edits.
    subjects_in_order = [entry for row in subject_rows for entry in row]
    teachers_in_order = [entry for row in teacher_rows for entry in row if any(entry)]
    if len(teachers_in_order) < len(subjects_in_order):
        expanded = [entry for line in str(teacher_text or "").split("\n")
                    for entry in semester_teachers(line, expand_joined=True) if any(entry)]
        if len(expanded) == len(subjects_in_order):
            teachers_in_order = expanded
    aligned_in_order = can_align_teachers(subjects_in_order, teachers_in_order)
    ordered_teachers = iter(teachers_in_order)
    teachers_by_room = {}
    for teacher, room in teachers_in_order:
        if teacher and room:
            teachers_by_room.setdefault(room.casefold(), set()).add(teacher)

    entries = []
    issues = set()
    for index, subjects in enumerate(subject_rows):
        for subject, room, weeks in subjects:
            ordered_teacher = next(ordered_teachers) if aligned_in_order else ("", "")
            if not weeks:
                issues.add("занятие без номера недели пропущено")
                continue
            if aligned:
                teacher, teacher_room = teacher_rows[index][0]
            elif aligned_in_order:
                teacher, teacher_room = ordered_teacher
            else:
                candidates = teachers_by_room.get(room.casefold(), ())
                teacher = next(iter(candidates)) if len(candidates) == 1 else ""
                teacher_room = ""
                if not teacher and teacher_text:
                    issues.add("преподавателя для части занятий не удалось сопоставить")
            if known_room(room) and known_room(teacher_room) and room.casefold() != teacher_room.casefold():
                issues.add("кабинеты в строках предмета и преподавателя различаются")
            if not known_room(room) and (known_room(teacher_room) or not room):
                room = teacher_room
            if not known_room(room) and len(room_rows) == len(subject_rows) and len(subjects) == 1:
                candidate = display_room(room_rows[index])
                if known_room(candidate) and re.fullmatch(ROOM_LABEL, candidate, re.IGNORECASE):
                    room = candidate
            entries.append((subject, room, teacher, weeks))
    return entries, issues


def can_align_teachers(subjects, teachers):
    if len(subjects) != len(teachers):
        return False
    room_pairs = [(room.casefold(), teacher_room.casefold())
                  for (_, room, _), (_, teacher_room) in zip(subjects, teachers)]
    shifted_rooms = {teacher_room for room, teacher_room in room_pairs
                     if room != teacher_room and known_room(teacher_room)}
    if any(room in shifted_rooms and room != teacher_room for room, teacher_room in room_pairs):
        return False
    pairs = [(room, teacher_room) for room, teacher_room in room_pairs if known_room(room) and known_room(teacher_room)]
    conflicts = [room for room, teacher_room in pairs if room != teacher_room]
    if not conflicts:
        return True
    # Resolve a single discrepancy when another room confirms the order.
    return len(conflicts) == 1 and any(room == teacher_room for room, teacher_room in pairs)


def semester_teachers(line, *, expand_joined=False):
    line = TEACHER_ROOM_SEPARATOR.sub(r"\g<room>  ", line.strip(" \t\r,"))
    if expand_joined:
        line = JOINED_TEACHERS.sub("  ", line)
    teachers = []
    for part in TEACHER_SEPARATOR.split(line):
        teacher, room = teacher_and_room(part)
        if not teacher and room and teachers and teachers[-1][0] and not teachers[-1][1]:
            teachers[-1] = (teachers[-1][0], room)
        else:
            teachers.append((teacher, room))
    return teachers


def semester_subjects(line):
    cursor = 0
    for match in SEMESTER_WEEKS.finditer(line):
        subject, room = split_subject_room(line[cursor:match.start()])
        if not subject:
            raise ValueError
        cursor = match.end()
        correction = ROOM_CORRECTION.match(line, cursor)
        if correction:
            room = display_room(correction["room"])
            cursor = correction.end()
        flow = re.match(r"[ \t,]*поток\b", line[cursor:], re.IGNORECASE)
        if flow:
            subject += " (поток)"
            cursor += flow.end()
        weeks = week_numbers(",".join(re.findall(WEEK_TERM, match["weeks"])))
        yield subject, room, weeks
    if line[cursor:].strip(" \t\r,"):
        subject, room = split_subject_room(line[cursor:])
        yield subject, room, set()


def split_subject_room(text):
    parts = [part.strip(" \t\r,-–—") for part in text.split("·") if part.strip(" \t\r,-–—")]
    if not parts:
        return "", ""
    joint = parts.pop() if len(parts) > 1 and parts[-1].startswith("с ") else ""
    match = ROOM_SUFFIX.search(parts[-1])
    room = display_room(match["room"]) if match else ""
    if match:
        parts[-1] = parts[-1][:match.start()].strip()
    return " · ".join(part for part in [*parts, joint] if part), room


def display_room(value):
    text = re.sub(r"^([0-9]+)\.0$", r"\1", clean(value))
    return re.sub(r"^(Ц)[ \t]+(?=\d+$)", r"\1", text, flags=re.IGNORECASE)


def known_room(room):
    return room.casefold() not in UNKNOWN_ROOMS


def teacher_and_room(value):
    teacher, dot, room = value.strip(" ,").partition("·")
    if dot:
        return teacher.strip(), display_room(room)
    match = ROOM_SUFFIX.search(teacher)
    if match:
        return teacher[:match.start()].rstrip(" :-"), display_room(match["room"])
    return teacher, ""


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
