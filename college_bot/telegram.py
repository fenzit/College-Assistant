import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

logger = logging.getLogger(__name__)


async def send_text(
    bot: Bot, user_id: int, text: str, *, reply_markup: InlineKeyboardMarkup | None = None,
) -> str:
    for attempt in range(3):
        try:
            await bot.send_message(user_id, text, reply_markup=reply_markup)
            return "sent"
        except TelegramRetryAfter as exc:
            if attempt < 2:
                await asyncio.sleep(exc.retry_after)
        except (TelegramForbiddenError, TelegramBadRequest):
            return "failed"
        except TelegramAPIError as exc:
            logger.warning("Telegram не подтвердил отправку: %s", type(exc).__name__)
            return "unknown"
    return "failed"
