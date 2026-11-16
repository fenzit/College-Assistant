import json
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


def group_key(name: str) -> str:
    return " ".join(name.split()).casefold()


@dataclass(frozen=True)
class Settings:
    token: str
    admin_id: int
    teacher_groups: dict[int, frozenset[str]]
    timezone: ZoneInfo
    data_dir: Path

    def role(self, user_id: int) -> str:
        if user_id == self.admin_id:
            return "admin"
        if user_id in self.teacher_groups:
            return "teacher"
        return "student"

    def can_send(self, user_id: int, group: str | None) -> bool:
        if user_id == self.admin_id:
            return True
        return group is not None and group_key(group) in self.teacher_groups.get(user_id, ())

    def question_recipients(self, group: str) -> list[int]:
        key = group_key(group)
        teachers = [user_id for user_id, groups in self.teacher_groups.items() if key in groups]
        return list(dict.fromkeys([*teachers, self.admin_id]))


def load_settings() -> Settings:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Укажите BOT_TOKEN в .env")
    try:
        admin_id = int(os.getenv("ADMIN_ID", ""))
        if admin_id <= 0:
            raise ValueError
    except ValueError:
        raise ValueError("Укажите числовой Telegram ID администратора в ADMIN_ID") from None

    try:
        raw = json.loads(os.getenv("TEACHER_GROUPS", "{}"))
        if not isinstance(raw, dict):
            raise ValueError
        teacher_groups = {}
        for raw_id, groups in raw.items():
            teacher_id = int(raw_id)
            if teacher_id <= 0 or not isinstance(groups, list):
                raise ValueError
            if any(not isinstance(group, str) or not group_key(group) for group in groups):
                raise ValueError
            teacher_groups[teacher_id] = frozenset(group_key(group) for group in groups)
    except (TypeError, ValueError):
        raise ValueError('Укажите TEACHER_GROUPS в формате {"123456789": ["СВ-21"]}') from None

    return Settings(
        token=token,
        admin_id=admin_id,
        teacher_groups=teacher_groups,
        timezone=ZoneInfo(os.getenv("COLLEGE_TIMEZONE", "Asia/Almaty")),
        data_dir=Path(os.getenv("DATA_DIR", "data")),
    )
