"""The publisher: every few seconds it sends the posts whose time has come.

At most once: a post is leased as "sending" before the request to Telegram; if the process dies in
between, the post becomes "unknown" (see store.py) instead of being sent twice. Temporary problems are
retried (Telegram's own `retry_after` is obeyed exactly), permanent ones are shown to the editor.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from . import formatting, schedule
from .config import Settings
from .store import Post, Store
from .telegram import Sender, classify

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
RETRY_BASE = 30.0


class Publisher:
    def __init__(self, store: Store, settings: Settings, sender: Sender, clock: Callable[[], float] = time.time):
        self.store = store
        self.settings = settings
        self.sender = sender
        self.clock = clock

    async def run(self, stop: asyncio.Event) -> None:
        log.info("publisher started")
        while not stop.is_set():
            try:
                busy = await self.tick()
            except Exception:  # one broken post must not stop the publisher
                log.exception("publisher error")
                busy = False
            if not busy:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2.0)
                except TimeoutError:
                    pass

    async def tick(self) -> bool:
        for post_id in await self.store.reap(self.clock()):
            log.warning("post %s: publisher stopped while sending, marked unknown", post_id)
        post = await self.store.claim(self.clock())
        if post is None:
            return False
        await self.publish(post)
        return True

    async def publish(self, post: Post) -> None:
        channel = await self.store.channel(post.channel_id)
        html = formatting.to_telegram_html(post.text)
        buttons, _ = formatting.parse_buttons(post.buttons)
        photo = self.settings.media_dir / post.photo if post.photo else None
        if photo is not None and not photo.exists():
            await self.store.failed(post.id, "the photo file is missing", self.clock())
            return
        try:
            message_id = await self.sender.send(channel.chat_id, html, photo, buttons, post.silent)
        except Exception as exc:  # noqa: BLE001 - every error is classified below
            outcome = classify(exc)
            if outcome.retry_after is None or post.attempts >= MAX_ATTEMPTS:
                log.error("post %s failed: %s", post.id, outcome.message)
                await self.store.failed(post.id, outcome.message, self.clock())
            else:
                delay = outcome.retry_after or RETRY_BASE * 2 ** (post.attempts - 1)
                log.warning("post %s: %s, retry in %.0f s", post.id, outcome.message, delay)
                await self.store.send_later(post.id, self.clock() + delay, outcome.message, self.clock())
            return
        next_at = schedule.next_time(post.publish_at or self.clock(), post.repeat, self.settings.tz)
        await self.store.published(post.id, message_id, self.clock(), next_at)
        log.info("post %s published to %s as message %s", post.id, channel.title, message_id)
        if post.pin:
            try:
                await self.sender.pin(channel.chat_id, message_id, post.silent)
            except Exception as exc:  # noqa: BLE001 - the post is out; pinning is a bonus
                log.warning("post %s: could not pin: %s", post.id, classify(exc).message)
