"""SQLite storage: users, channels, posts and their life cycle, the history of every action.

    draft ──submit──► pending ──approve──► scheduled ──claim──► sending ──► published ──► deleted
      ▲                  │ reject            │ cancel  ▲            │
      └──────────────────┴───────────────────┘        │            ├─ temporary error ─► scheduled (later)
                                                      │            ├─ permanent error ─► failed ──retry──┘
                                                      │            └─ lease expired ──► unknown
                                                      └───────── "send again" (a person checked the channel)

Why "unknown" instead of sending again: if the process died after Telegram accepted the post but before
we saved that, a blind retry would publish the post twice. A double post in a channel is worse than a
person spending ten seconds to look and press "mark as published" or "send again".
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

STATUSES = ("draft", "pending", "scheduled", "sending", "published", "failed", "unknown", "deleted")
ROLES = ("admin", "editor", "author")
LEASE = 120  # seconds one publisher may spend on one post

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'editor', 'author')),
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL UNIQUE,
    title TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    author_id INTEGER REFERENCES users(id),
    text TEXT NOT NULL DEFAULT '',
    photo TEXT NOT NULL DEFAULT '',
    buttons TEXT NOT NULL DEFAULT '',
    repeat TEXT NOT NULL DEFAULT '',
    pin INTEGER NOT NULL DEFAULT 0,
    silent INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',
    publish_at REAL,
    next_try_at REAL NOT NULL DEFAULT 0,
    lease_until REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER,
    published_at REAL,
    last_error TEXT NOT NULL DEFAULT '',
    review_note TEXT NOT NULL DEFAULT '',
    approved_by INTEGER REFERENCES users(id),
    series_id INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_posts_due ON posts (status, publish_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    user_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_usage (day TEXT PRIMARY KEY, count INTEGER NOT NULL);
"""


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


@dataclass
class User:
    id: int
    email: str
    name: str
    role: str
    password_hash: str

    @property
    def can_publish(self) -> bool:
        return self.role in ("admin", "editor")


@dataclass
class Channel:
    id: int
    chat_id: int
    title: str
    username: str


@dataclass
class Post:
    id: int
    channel_id: int
    author_id: int | None
    text: str
    photo: str
    buttons: str
    repeat: str
    pin: bool
    silent: bool
    status: str
    publish_at: float | None
    attempts: int
    message_id: int | None
    published_at: float | None
    last_error: str
    review_note: str
    approved_by: int | None
    series_id: int | None
    created_at: float
    updated_at: float
    channel_title: str = ""
    author_name: str = ""

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> Post:
        d = dict(row)
        return cls(
            id=d["id"], channel_id=d["channel_id"], author_id=d["author_id"], text=d["text"], photo=d["photo"],
            buttons=d["buttons"], repeat=d["repeat"], pin=bool(d["pin"]), silent=bool(d["silent"]),
            status=d["status"], publish_at=d["publish_at"], attempts=d["attempts"], message_id=d["message_id"],
            published_at=d["published_at"], last_error=d["last_error"], review_note=d["review_note"],
            approved_by=d["approved_by"], series_id=d["series_id"], created_at=d["created_at"],
            updated_at=d["updated_at"], channel_title=d.get("channel_title") or "",
            author_name=d.get("author_name") or "",
        )


POST_SELECT = ("SELECT p.*, c.title AS channel_title, u.name AS author_name FROM posts p "
               "JOIN channels c ON c.id = p.channel_id LEFT JOIN users u ON u.id = p.author_id")
EDITABLE = ("draft", "pending", "scheduled", "failed")
MOVE_COLUMNS = {"publish_at", "approved_by", "review_note", "next_try_at", "last_error", "message_id"}


class Store:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn
        self.lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path | str) -> Store:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA busy_timeout = 5000")
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    async def _event(self, post_id: int, user_id: int | None, action: str, detail: str, now: float) -> None:
        await self.conn.execute("INSERT INTO events (post_id, user_id, action, detail, at) VALUES (?, ?, ?, ?, ?)",
                                (post_id, user_id, action, detail[:500], now))

    # ------------------------------------------------------------ users and channels
    async def add_user(self, email: str, name: str, role: str, password_hash: str, now: float) -> int:
        async with self.lock:
            try:
                cur = await self.conn.execute(
                    "INSERT INTO users (email, name, role, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                    (email.strip(), name.strip(), role, password_hash, now))
            except sqlite3.IntegrityError:
                raise Conflict(f"user {email} already exists") from None
            await self.conn.commit()
            return int(cur.lastrowid)

    async def set_password(self, email: str, password_hash: str) -> bool:
        async with self.lock:
            cur = await self.conn.execute("UPDATE users SET password_hash = ? WHERE email = ?",
                                          (password_hash, email.strip()))
            await self.conn.commit()
            return cur.rowcount > 0

    async def user(self, user_id: int) -> User | None:
        cur = await self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        r = await cur.fetchone()
        return User(r["id"], r["email"], r["name"], r["role"], r["password_hash"]) if r else None

    async def user_by_email(self, email: str) -> User | None:
        cur = await self.conn.execute("SELECT * FROM users WHERE email = ?", (email.strip(),))
        r = await cur.fetchone()
        return User(r["id"], r["email"], r["name"], r["role"], r["password_hash"]) if r else None

    async def users(self) -> list[User]:
        cur = await self.conn.execute("SELECT * FROM users ORDER BY id")
        return [User(r["id"], r["email"], r["name"], r["role"], r["password_hash"]) for r in await cur.fetchall()]

    async def add_channel(self, chat_id: int, title: str, username: str, now: float) -> int:
        async with self.lock:
            cur = await self.conn.execute(
                "INSERT INTO channels (chat_id, title, username, added_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title, username = excluded.username RETURNING id",
                (chat_id, title, username, now))
            row = await cur.fetchone()
            await self.conn.commit()
            return int(row["id"])

    async def channels(self) -> list[Channel]:
        cur = await self.conn.execute("SELECT * FROM channels ORDER BY title")
        return [Channel(r["id"], r["chat_id"], r["title"], r["username"]) for r in await cur.fetchall()]

    async def channel(self, channel_id: int) -> Channel:
        cur = await self.conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
        r = await cur.fetchone()
        if r is None:
            raise NotFound(f"channel {channel_id}")
        return Channel(r["id"], r["chat_id"], r["title"], r["username"])

    # ------------------------------------------------------------ posts
    async def create_post(self, author_id: int | None, fields: dict[str, Any], now: float) -> int:
        async with self.lock:
            cur = await self.conn.execute(
                "INSERT INTO posts (channel_id, author_id, text, photo, buttons, repeat, pin, silent, publish_at, "
                "created_at, updated_at) VALUES (:channel_id, :author_id, :text, :photo, :buttons, :repeat, :pin, "
                ":silent, :publish_at, :now, :now)",
                {**fields, "author_id": author_id, "now": now})
            post_id = int(cur.lastrowid)
            await self._event(post_id, author_id, "created", "", now)
            await self.conn.commit()
            return post_id

    async def get(self, post_id: int) -> Post:
        cur = await self.conn.execute(POST_SELECT + " WHERE p.id = ?", (post_id,))
        row = await cur.fetchone()
        if row is None:
            raise NotFound(f"post {post_id}")
        return Post.from_row(row)

    async def update_post(self, post_id: int, user_id: int, fields: dict[str, Any], now: float) -> None:
        """Content changes. A scheduled or failed post goes back to draft when an author (not an editor) edits it."""
        async with self.lock:
            post = await self.get(post_id)
            if post.status not in EDITABLE:
                raise Conflict(f"a {post.status} post cannot be edited here")
            await self.conn.execute(
                "UPDATE posts SET channel_id = :channel_id, text = :text, photo = :photo, buttons = :buttons, "
                "repeat = :repeat, pin = :pin, silent = :silent, publish_at = :publish_at, updated_at = :now "
                "WHERE id = :id", {**fields, "id": post_id, "now": now})
            await self._event(post_id, user_id, "edited", "", now)
            await self.conn.commit()

    async def update_text(self, post_id: int, user_id: int, text: str, buttons: str, now: float) -> None:
        """A published post was edited in the channel: keep our copy in sync."""
        async with self.lock:
            await self.conn.execute("UPDATE posts SET text = ?, buttons = ?, updated_at = ? WHERE id = ?",
                                    (text, buttons, now, post_id))
            await self._event(post_id, user_id, "edited in channel", "", now)
            await self.conn.commit()

    async def move(self, post_id: int, user_id: int | None, allowed_from: tuple[str, ...], status: str,
                   action: str, now: float, detail: str = "", **extra: Any) -> Post:
        """A status change that is only valid from some statuses (checked inside the same write)."""
        # statuses and column names come from our code, never from a request: refuse anything else
        if not set(allowed_from) <= set(STATUSES) or status not in STATUSES or not set(extra) <= MOVE_COLUMNS:
            raise ValueError("unknown status or column")
        sets = "".join(f", {k} = :{k}" for k in extra)
        allowed = ", ".join(f"'{s}'" for s in allowed_from)
        async with self.lock:
            cur = await self.conn.execute(
                f"UPDATE posts SET status = :status, updated_at = :now{sets} WHERE id = :id AND status IN ({allowed})",
                {"status": status, "now": now, "id": post_id, **extra})
            if cur.rowcount == 0:
                await self.conn.rollback()
                post = await self.get(post_id)
                raise Conflict(f"the post is {post.status}: cannot {action}")
            await self._event(post_id, user_id, action, detail, now)
            await self.conn.commit()
        return await self.get(post_id)

    async def posts(self, status: str | None = None, channel_id: int | None = None, author_id: int | None = None,
                    since: float | None = None, until: float | None = None, limit: int = 300) -> list[Post]:
        sql, args = POST_SELECT + " WHERE 1 = 1", []
        for cond, value in (("p.status = ?", status), ("p.channel_id = ?", channel_id), ("p.author_id = ?", author_id),
                            ("COALESCE(p.published_at, p.publish_at) >= ?", since),
                            ("COALESCE(p.published_at, p.publish_at) < ?", until)):
            if value is not None:
                sql += " AND " + cond
                args.append(value)
        sql += " ORDER BY COALESCE(p.published_at, p.publish_at, p.updated_at) DESC LIMIT ?"
        cur = await self.conn.execute(sql, (*args, limit))
        return [Post.from_row(r) for r in await cur.fetchall()]

    async def counts(self) -> dict[str, int]:
        cur = await self.conn.execute("SELECT status, COUNT(*) AS n FROM posts GROUP BY status")
        found = {r["status"]: r["n"] for r in await cur.fetchall()}
        return {s: found.get(s, 0) for s in STATUSES}

    async def events(self, post_id: int) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT e.*, u.name AS user_name FROM events e LEFT JOIN users u ON u.id = e.user_id "
            "WHERE e.post_id = ? ORDER BY e.id DESC", (post_id,))
        return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------ publishing
    async def claim(self, now: float) -> Post | None:
        """The most overdue scheduled post, leased to this publisher."""
        async with self.lock:
            cur = await self.conn.execute(
                "UPDATE posts SET status = 'sending', lease_until = ?, attempts = attempts + 1, updated_at = ? "
                "WHERE id = (SELECT id FROM posts WHERE status = 'scheduled' AND publish_at <= ? AND next_try_at <= ? "
                "ORDER BY publish_at, id LIMIT 1) RETURNING id", (now + LEASE, now, now, now))
            row = await cur.fetchone()
            await self.conn.commit()
        return await self.get(int(row["id"])) if row else None

    async def reap(self, now: float) -> list[int]:
        """Posts whose publisher died while sending: the outcome is unknown, a person decides."""
        async with self.lock:
            cur = await self.conn.execute(
                "UPDATE posts SET status = 'unknown', lease_until = NULL, updated_at = ?, last_error = "
                "'the publisher stopped while sending: check the channel, then mark as published or send again' "
                "WHERE status = 'sending' AND lease_until < ? RETURNING id", (now, now))
            ids = [int(r["id"]) for r in await cur.fetchall()]
            for post_id in ids:
                await self._event(post_id, None, "unknown", "publisher stopped while sending", now)
            await self.conn.commit()
            return ids

    async def published(self, post_id: int, message_id: int, now: float, next_at: float | None) -> int | None:
        """Marks the post as published; a repeating post gets its next copy. Returns the id of that copy."""
        async with self.lock:
            await self.conn.execute(
                "UPDATE posts SET status = 'published', message_id = ?, published_at = ?, lease_until = NULL, "
                "last_error = '', updated_at = ? WHERE id = ?", (message_id, now, now, post_id))
            await self._event(post_id, None, "published", f"message {message_id}", now)
            next_id = None
            if next_at is not None:
                cur = await self.conn.execute(
                    "INSERT INTO posts (channel_id, author_id, text, photo, buttons, repeat, pin, silent, status, "
                    "publish_at, approved_by, series_id, created_at, updated_at) SELECT channel_id, author_id, text, "
                    "photo, buttons, repeat, pin, silent, 'scheduled', ?, approved_by, COALESCE(series_id, id), ?, ? "
                    "FROM posts WHERE id = ?", (next_at, now, now, post_id))
                next_id = int(cur.lastrowid)
                await self._event(next_id, None, "scheduled", f"next run of post #{post_id}", now)
            await self.conn.commit()
            return next_id

    async def send_later(self, post_id: int, at: float, error: str, now: float) -> None:
        async with self.lock:
            await self.conn.execute(
                "UPDATE posts SET status = 'scheduled', next_try_at = ?, lease_until = NULL, last_error = ?, "
                "updated_at = ? WHERE id = ?", (at, error[:300], now, post_id))
            await self._event(post_id, None, "retry", error, now)
            await self.conn.commit()

    async def failed(self, post_id: int, error: str, now: float) -> None:
        async with self.lock:
            await self.conn.execute(
                "UPDATE posts SET status = 'failed', lease_until = NULL, last_error = ?, updated_at = ? WHERE id = ?",
                (error[:300], now, post_id))
            await self._event(post_id, None, "failed", error, now)
            await self.conn.commit()

    # ------------------------------------------------------------ AI budget
    async def take_ai_call(self, day: str, limit: int) -> bool:
        async with self.lock:
            cur = await self.conn.execute(
                "INSERT INTO ai_usage (day, count) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET count = count + 1 "
                "WHERE count < ? RETURNING count", (day, limit))
            row = await cur.fetchone()
            await self.conn.commit()
            return row is not None and limit > 0
