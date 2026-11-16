import asyncio
import logging
from contextlib import suppress
from logging.handlers import RotatingFileHandler

from aiogram import Bot

from .bot import create_dispatcher
from .broadcasts import Broadcasts
from .config import load_settings
from .database import Database


async def main():
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(settings.data_dir / "bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
        ],
    )
    db = Database(settings.data_dir / "college.sqlite3")
    db.initialize()
    async with Bot(settings.token) as bot:
        broadcasts = Broadcasts(db)
        worker = asyncio.create_task(broadcasts.run(bot))
        try:
            await create_dispatcher(settings, db, broadcasts).start_polling(bot, close_bot_session=False)
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker


if __name__ == "__main__":
    asyncio.run(main())
