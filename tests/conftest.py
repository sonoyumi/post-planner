"""Shared fixtures: settings in a temp folder, a real SQLite store, a fake Telegram that records calls."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiogram.methods import SendMessage

from post_planner.config import Settings
from post_planner.formatting import Button
from post_planner.store import Store
from post_planner.telegram import ChannelInfo, ChannelProblem

ROME = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=ROME).timestamp()  # Thursday 1 October 2026, 08:00 in Rome
CHAT = -1001234567890
METHOD = SendMessage(chat_id=CHAT, text="x")


def local(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=ROME).timestamp()


class FakeSender:
    """Records what would go to Telegram; `fail` holds exceptions to raise on the next sends."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.pinned: list[int] = []
        self.edited: list[dict] = []
        self.deleted: list[int] = []
        self.fail: list[Exception] = []
        self.pin_fail: Exception | None = None
        self.next_id = 100

    async def send(self, chat_id: int, html: str, photo: Path | None, buttons: list[Button], silent: bool) -> int:
        if self.fail:
            raise self.fail.pop(0)
        self.next_id += 1
        self.sent.append({"chat_id": chat_id, "html": html, "photo": photo, "buttons": buttons, "silent": silent,
                          "message_id": self.next_id})
        return self.next_id

    async def pin(self, chat_id: int, message_id: int, silent: bool) -> None:
        if self.pin_fail:
            raise self.pin_fail
        self.pinned.append(message_id)

    async def edit(self, chat_id: int, message_id: int, html: str, has_photo: bool, buttons: list[Button]) -> None:
        if self.fail:
            raise self.fail.pop(0)
        self.edited.append({"message_id": message_id, "html": html, "buttons": buttons})

    async def delete(self, chat_id: int, message_id: int) -> None:
        self.deleted.append(message_id)

    async def channel(self, ref: str) -> ChannelInfo:
        if ref.lstrip("@") == "laurin_news":
            return ChannelInfo(CHAT, "Trattoria Laurin", "laurin_news")
        raise ChannelProblem(f"{ref}: channel not found or the bot is not in it — "
                             "add the bot as an administrator first")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(secret_key="test-secret-" + "x" * 32, bot_token="123456:" + "A" * 35, timezone="Europe/Rome",
                    data_dir=tmp_path / "data", team_name="Laurin Social")


@pytest.fixture
async def store():
    s = await Store.open(":memory:")
    yield s
    await s.close()


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


async def make_post(store: Store, channel_id: int, status: str = "scheduled", when: float | None = None,
                    **fields) -> int:
    data = {"channel_id": channel_id, "text": "**Menu** del giorno", "photo": "", "buttons": "", "repeat": "",
            "pin": 0, "silent": 0, "publish_at": when if when is not None else NOW - 60, **fields}
    post_id = await store.create_post(None, data, NOW - 3600)
    if status != "draft":
        await store.move(post_id, None, ("draft",), status, "test", NOW - 3600)
    return post_id
