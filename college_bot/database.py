import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


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
