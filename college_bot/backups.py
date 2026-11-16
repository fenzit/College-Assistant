import asyncio
import logging
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone

from .database import Database

logger = logging.getLogger(__name__)
BACKUP_INTERVAL = 7 * 24 * 60 * 60
CHECK_INTERVAL = 60 * 60


def backup_if_due(db: Database):
    directory = db.path.parent / "backups"
    directory.mkdir(exist_ok=True)
    latest = max((item.stat().st_mtime for item in directory.glob("college-*.sqlite3")), default=0)
    if time.time() - latest < BACKUP_INTERVAL:
        return None

    filename = datetime.now(timezone.utc).strftime("college-%Y-%m-%d_%H%M%SZ.sqlite3")
    destination = directory / filename
    temporary = destination.with_suffix(".tmp")
    try:
        with db.connection() as source, closing(sqlite3.connect(temporary)) as snapshot:
            source.backup(snapshot)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    logger.info("Создана резервная копия %s", filename)
    return destination


async def backup_loop(db: Database):
    while True:
        try:
            await asyncio.to_thread(backup_if_due, db)
        except Exception:
            logger.exception("Не удалось создать резервную копию")
        await asyncio.sleep(CHECK_INTERVAL)
