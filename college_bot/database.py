import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from .schedule import Schedule


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:
                yield conn

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS groups (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    name_key TEXT UNIQUE NOT NULL,
                    curator TEXT
                );
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    group_id INTEGER REFERENCES groups(id),
                    role TEXT NOT NULL,
                    registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS users_group ON users(group_id);
                CREATE TABLE IF NOT EXISTS schedule_days (
                    group_id INTEGER NOT NULL REFERENCES groups(id),
                    day TEXT NOT NULL,
                    PRIMARY KEY (group_id, day)
                );
                CREATE TABLE IF NOT EXISTS schedule (
                    id INTEGER PRIMARY KEY,
                    group_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    period INTEGER NOT NULL,
                    starts_at TEXT NOT NULL,
                    ends_at TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    room TEXT NOT NULL,
                    teacher TEXT NOT NULL,
                    FOREIGN KEY (group_id, day) REFERENCES schedule_days(group_id, day) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS schedule_by_day ON schedule(group_id, day, period);
            """)

    def register(self, user_id: int, name: str, role: str) -> None:
        with self.connection() as conn:
            conn.execute("""
                INSERT INTO users (telegram_id, name, role) VALUES (?, ?, ?)
                ON CONFLICT (telegram_id) DO UPDATE SET name = excluded.name, role = excluded.role
            """, (user_id, name, role))

    def groups(self) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute("SELECT id, name FROM groups ORDER BY name_key").fetchall()

    def group(self, group_id: int) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute("SELECT id, name FROM groups WHERE id = ?", (group_id,)).fetchone()

    def user_group(self, user_id: int) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute("""
                SELECT groups.id, groups.name FROM users JOIN groups ON groups.id = users.group_id
                WHERE users.telegram_id = ?
            """, (user_id,)).fetchone()

    def set_group(self, user_id: int, group_id: int) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE users SET group_id = ? WHERE telegram_id = ?", (group_id, user_id))

    def replace_schedule(self, schedule: Schedule) -> None:
        with self.connection() as conn:
            conn.executemany(
                "INSERT INTO groups (name_key, name) VALUES (?, ?) ON CONFLICT (name_key) DO NOTHING",
                schedule.groups.items(),
            )
            group_ids = {
                row["name_key"]: row["id"] for row in conn.execute("SELECT id, name_key FROM groups")
            }
            covered = [(group_ids[key], day.isoformat()) for key, day in schedule.coverage]
            conn.executemany("DELETE FROM schedule_days WHERE group_id = ? AND day = ?", covered)
            conn.executemany("INSERT INTO schedule_days (group_id, day) VALUES (?, ?)", covered)
            conn.executemany("""
                INSERT INTO schedule (group_id, day, period, starts_at, ends_at, subject, room, teacher)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                (group_ids[lesson.group], lesson.day.isoformat(), lesson.period, lesson.starts_at,
                 lesson.ends_at, lesson.subject, lesson.room, lesson.teacher)
                for lesson in schedule.lessons
            ))

    def timetable(self, group_id: int, first, last) -> dict[str, list[sqlite3.Row]]:
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT days.day, s.period, s.starts_at, s.ends_at, s.subject, s.room, s.teacher
                FROM schedule_days AS days
                LEFT JOIN schedule AS s ON s.group_id = days.group_id AND s.day = days.day
                WHERE days.group_id = ? AND days.day BETWEEN ? AND ?
                ORDER BY days.day, s.period, s.starts_at, s.id
            """, (group_id, first.isoformat(), last.isoformat())).fetchall()
        days = {}
        for row in rows:
            lessons = days.setdefault(row["day"], [])
            if row["period"] is not None:
                lessons.append(row)
        return days
