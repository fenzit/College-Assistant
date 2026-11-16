import asyncio
import logging
from contextlib import suppress
from logging.handlers import RotatingFileHandler

from aiogram import Bot

from .backups import backup_loop
from .bot import create_dispatcher
from .broadcasts import Broadcasts
from .config import load_settings
from .database import Database


class LogFormatter(logging.Formatter):
    def __init__(self, token: str):
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self.token = token

    def format(self, record):
        return super().format(record).replace(self.token, "[скрыто]")


async def main():
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(settings.data_dir / "bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
    ]
    formatter = LogFormatter(settings.token)
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers)
    db = Database(settings.data_dir / "college.sqlite3")
    db.initialize()
    async with Bot(settings.token) as bot:
        broadcasts = Broadcasts(db)
        workers = [
            asyncio.create_task(broadcasts.run(bot)),
            asyncio.create_task(backup_loop(db)),
        ]
        try:
            await create_dispatcher(settings, db, broadcasts).start_polling(bot, close_bot_session=False)
        finally:
            for worker in workers:
                worker.cancel()
            for worker in workers:
                with suppress(asyncio.CancelledError):
                    await worker


if __name__ == "__main__":
    asyncio.run(main())
