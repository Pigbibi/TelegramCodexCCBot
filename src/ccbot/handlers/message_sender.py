"""Safe message sending helpers with MarkdownV2 fallback.

Provides utility functions for sending Telegram messages with automatic
format conversion and fallback to plain text on failure.

Uses telegramify-markdown for MarkdownV2 formatting.

Functions:
  - send_with_fallback: Send with formatting → plain text fallback
  - send_photo: Photo sending (single or media group)
  - safe_reply: Reply with formatting, fallback to plain text
  - safe_edit: Edit message with formatting, fallback to plain text
  - safe_send: Send message with formatting, fallback to plain text

Rate limiting is handled globally by AIORateLimiter on the Application.
RetryAfter exceptions are re-raised so callers (queue worker) can handle them.
"""

import asyncio
import io
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from telegram import Bot, InputMediaPhoto, LinkPreviewOptions, Message
from telegram.error import NetworkError, RetryAfter

from ..markdown_v2 import convert_markdown
from ..transcript_parser import TranscriptParser

logger = logging.getLogger(__name__)

_CONNECT_ERROR_RETRY_DELAYS = (0.5, 1.0)


def strip_sentinels(text: str) -> str:
    """Strip expandable quote sentinel markers for plain text fallback."""
    for s in (
        TranscriptParser.EXPANDABLE_QUOTE_START,
        TranscriptParser.EXPANDABLE_QUOTE_END,
    ):
        text = text.replace(s, "")
    return text


def _ensure_formatted(text: str) -> str:
    """Convert markdown to MarkdownV2."""
    return convert_markdown(text)


def _is_retryable_connect_error(exc: BaseException) -> bool:
    """Retry only transient connect failures before Telegram receives the request."""
    if not isinstance(exc, NetworkError):
        return False
    current: BaseException | None = exc
    while current is not None:
        if "ConnectError" in str(current):
            return True
        current = current.__cause__
    return False


async def _call_with_connect_retry(
    send_call: Callable[[], Awaitable[Any]],
    *,
    action: str,
) -> Any:
    """Retry Telegram sends when the client cannot establish a connection at all."""
    total_attempts = len(_CONNECT_ERROR_RETRY_DELAYS) + 1
    attempt = 0
    while True:
        attempt += 1
        try:
            return await send_call()
        except RetryAfter:
            raise
        except Exception as exc:
            if attempt >= total_attempts or not _is_retryable_connect_error(exc):
                raise
            logger.warning(
                "%s failed with transient connect error, retrying (%d/%d): %s",
                action,
                attempt + 1,
                total_attempts,
                exc,
            )
            await asyncio.sleep(_CONNECT_ERROR_RETRY_DELAYS[attempt - 1])


PARSE_MODE = "MarkdownV2"


# Disable link previews in all messages to reduce visual noise
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def send_with_fallback(
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs: Any,
) -> Message | None:
    """Send message with MarkdownV2, falling back to plain text on failure.

    Returns the sent Message on success, None on failure.
    RetryAfter is re-raised for caller handling.
    """
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        return await _call_with_connect_retry(
            lambda: bot.send_message(
                chat_id=chat_id,
                text=_ensure_formatted(text),
                parse_mode=PARSE_MODE,
                **kwargs,
            ),
            action=f"Send formatted message to {chat_id}",
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            return await _call_with_connect_retry(
                lambda: bot.send_message(
                    chat_id=chat_id, text=strip_sentinels(text), **kwargs
                ),
                action=f"Send plain message to {chat_id}",
            )
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return None


async def send_photo(
    bot: Bot,
    chat_id: int,
    image_data: list[tuple[str, bytes]],
    **kwargs: Any,
) -> None:
    """Send photo(s) to chat. Sends as media group if multiple images.

    Rate limiting is handled globally by AIORateLimiter on the Application.

    Args:
        bot: Telegram Bot instance
        chat_id: Target chat ID
        image_data: List of (media_type, raw_bytes) tuples
        **kwargs: Extra kwargs passed to send_photo/send_media_group
    """
    if not image_data:
        return
    try:
        if len(image_data) == 1:
            _media_type, raw_bytes = image_data[0]
            await bot.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(raw_bytes),
                **kwargs,
            )
        else:
            media = [
                InputMediaPhoto(media=io.BytesIO(raw_bytes))
                for _media_type, raw_bytes in image_data
            ]
            await bot.send_media_group(
                chat_id=chat_id,
                media=media,
                **kwargs,
            )
    except RetryAfter:
        raise
    except Exception as e:
        logger.error("Failed to send photo to %d: %s", chat_id, e)


async def safe_reply(message: Message, text: str, **kwargs: Any) -> Message:
    """Reply with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        return await _call_with_connect_retry(
            lambda: message.reply_text(
                _ensure_formatted(text),
                parse_mode=PARSE_MODE,
                **kwargs,
            ),
            action="Reply with formatting",
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            return await _call_with_connect_retry(
                lambda: message.reply_text(strip_sentinels(text), **kwargs),
                action="Reply in plain text",
            )
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to reply: {e}")
            raise


async def safe_edit(target: Any, text: str, **kwargs: Any) -> None:
    """Edit message with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        await _call_with_connect_retry(
            lambda: target.edit_message_text(
                _ensure_formatted(text),
                parse_mode=PARSE_MODE,
                **kwargs,
            ),
            action="Edit formatted message",
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            await _call_with_connect_retry(
                lambda: target.edit_message_text(strip_sentinels(text), **kwargs),
                action="Edit plain message",
            )
        except RetryAfter:
            raise
        except Exception as e:
            logger.error("Failed to edit message: %s", e)


async def safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    message_thread_id: int | None = None,
    **kwargs: Any,
) -> None:
    """Send message with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    if message_thread_id is not None:
        kwargs.setdefault("message_thread_id", message_thread_id)
    try:
        await _call_with_connect_retry(
            lambda: bot.send_message(
                chat_id=chat_id,
                text=_ensure_formatted(text),
                parse_mode=PARSE_MODE,
                **kwargs,
            ),
            action=f"Send formatted message to {chat_id}",
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            await _call_with_connect_retry(
                lambda: bot.send_message(
                    chat_id=chat_id, text=strip_sentinels(text), **kwargs
                ),
                action=f"Send plain message to {chat_id}",
            )
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
