import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Literal

from .config import group_key
from .schedule import Schedule

Directory = Literal["contacts", "faq"]


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
                CREATE TABLE IF NOT EXISTS announcements (
                    id INTEGER PRIMARY KEY,
                    text TEXT NOT NULL,
                    group_id INTEGER REFERENCES groups(id),
                    author_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    reported INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    announcement_id INTEGER NOT NULL REFERENCES announcements(id),
                    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    status TEXT NOT NULL DEFAULT 'pending',
                    PRIMARY KEY (announcement_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS deliveries_pending ON deliveries(announcement_id, status, user_id);
                CREATE TABLE IF NOT EXISTS contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    details TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS faq (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    assignee_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                );
            """)
            conn.execute("UPDATE questions SET status = 'unknown' WHERE status = 'replying'")

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

    def group_by_name(self, name: str) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute("SELECT id, name FROM groups WHERE name_key = ?", (group_key(name),)).fetchone()

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

    def announcements(self, group_id: int | None) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute("""
                SELECT a.text, a.created_at, g.name
                FROM announcements AS a LEFT JOIN groups AS g ON g.id = a.group_id
                WHERE a.group_id IS NULL OR a.group_id = ?
                ORDER BY a.id DESC LIMIT 5
            """, (group_id,)).fetchall()

    def publish(self, text: str, group_id: int | None, author_id: int) -> int:
        with self.connection() as conn:
            announcement_id = conn.execute(
                "INSERT INTO announcements (text, group_id, author_id) VALUES (?, ?, ?)",
                (text, group_id, author_id),
            ).lastrowid
            cursor = conn.execute("""
                INSERT INTO deliveries (announcement_id, user_id)
                SELECT ?, telegram_id FROM users WHERE ? IS NULL OR group_id = ?
            """, (announcement_id, group_id, group_id))
            return cursor.rowcount

    def next_announcement(self) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute("""
                SELECT a.id, a.text, a.author_id, g.name FROM announcements AS a
                LEFT JOIN groups AS g ON g.id = a.group_id
                WHERE a.reported = 0 ORDER BY a.id LIMIT 1
            """).fetchone()

    def claim_delivery(self, announcement_id: int) -> int | None:
        with self.connection() as conn:
            row = conn.execute("""
                UPDATE deliveries SET status = 'sending'
                WHERE announcement_id = ? AND user_id = (
                    SELECT user_id FROM deliveries WHERE announcement_id = ? AND status = 'pending'
                    ORDER BY user_id LIMIT 1
                )
                RETURNING user_id
            """, (announcement_id, announcement_id)).fetchone()
            return row["user_id"] if row else None

    def finish_delivery(self, announcement_id: int, user_id: int, status: str) -> None:
        with self.connection() as conn:
            conn.execute("""
                UPDATE deliveries SET status = ?
                WHERE announcement_id = ? AND user_id = ?
            """, (status, announcement_id, user_id))

    def recover_deliveries(self) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE deliveries SET status = 'unknown' WHERE status = 'sending'")

    def delivery_counts(self, announcement_id: int) -> dict[str, int]:
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT status, COUNT(*) AS count FROM deliveries WHERE announcement_id = ? GROUP BY status
            """, (announcement_id,)).fetchall()
            return {row["status"]: row["count"] for row in rows}

    def mark_reported(self, announcement_id: int) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE announcements SET reported = 1 WHERE id = ?", (announcement_id,))

    def statistics(self) -> tuple[int, list[sqlite3.Row]]:
        with self.connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            rows = conn.execute("""
                SELECT g.name, COUNT(u.telegram_id) AS count
                FROM groups AS g LEFT JOIN users AS u ON g.id = u.group_id
                GROUP BY g.id ORDER BY g.name_key
            """).fetchall()
            return total, rows

    def directory(self, kind: Directory) -> list[sqlite3.Row]:
        query = (
            "SELECT id, title, details AS body FROM contacts ORDER BY id"
            if kind == "contacts" else "SELECT id, question AS title, answer AS body FROM faq ORDER BY id"
        )
        with self.connection() as conn:
            return conn.execute(query).fetchall()

    def directory_item(self, kind: Directory, item_id: int) -> sqlite3.Row | None:
        query = (
            "SELECT id, title, details AS body FROM contacts WHERE id = ?"
            if kind == "contacts" else "SELECT id, question AS title, answer AS body FROM faq WHERE id = ?"
        )
        with self.connection() as conn:
            return conn.execute(query, (item_id,)).fetchone()

    def save_directory_item(self, kind: Directory, item_id: int | None, title: str, body: str) -> bool:
        with self.connection() as conn:
            if item_id is None:
                query = (
                    "INSERT INTO contacts (title, details) VALUES (?, ?)"
                    if kind == "contacts" else "INSERT INTO faq (question, answer) VALUES (?, ?)"
                )
                conn.execute(query, (title, body))
                return True
            query = (
                "UPDATE contacts SET title = ?, details = ? WHERE id = ?"
                if kind == "contacts" else "UPDATE faq SET question = ?, answer = ? WHERE id = ?"
            )
            return conn.execute(query, (title, body, item_id)).rowcount == 1

    def delete_directory_item(self, kind: Directory, item_id: int) -> bool:
        query = "DELETE FROM contacts WHERE id = ?" if kind == "contacts" else "DELETE FROM faq WHERE id = ?"
        with self.connection() as conn:
            return conn.execute(query, (item_id,)).rowcount == 1

    def create_question(self, student_id: int, assignee_id: int, text: str) -> int:
        with self.connection() as conn:
            return conn.execute("""
                INSERT INTO questions (student_id, assignee_id, text) VALUES (?, ?, ?)
            """, (student_id, assignee_id, text)).lastrowid

    def assign_question(self, question_id: int, assignee_id: int) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE questions SET assignee_id = ? WHERE id = ?", (assignee_id, question_id))

    def question(self, question_id: int) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()

    def discard_question(self, question_id: int) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM questions WHERE id = ? AND status = 'pending'", (question_id,))

    def claim_reply(self, question_id: int) -> bool:
        with self.connection() as conn:
            return conn.execute("""
                UPDATE questions SET status = 'replying' WHERE id = ? AND status = 'pending'
            """, (question_id,)).rowcount == 1

    def finish_reply(self, question_id: int, status: str) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE questions SET status = ? WHERE id = ?", (status, question_id))
