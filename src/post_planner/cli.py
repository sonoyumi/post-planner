"""Command line.

post-planner add-user --email anna@shop.it --name Anna --role admin   create a user (asks for the password)
post-planner passwd anna@shop.it          set a new password
post-planner add-channel @my_channel      add a channel (the bot must be its admin)
post-planner serve [--no-publisher]       panel + publisher (one process)
post-planner publisher                    publisher only (next to `serve --no-publisher`)
post-planner check                        bot token and every channel's permissions
post-planner stats                        posts by status, problems
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import signal
import sys
import time
from collections.abc import Sequence

from . import __version__, security
from .ai import build_rewriter
from .config import ConfigError, Settings, load_settings
from .publisher import Publisher
from .store import ROLES, Conflict, Store
from .telegram import ChannelProblem, TelegramSender, classify

log = logging.getLogger("post_planner")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="post-planner", description="Content planner for Telegram channels")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    u = sub.add_parser("add-user", help="create a user of the panel")
    u.add_argument("--email", required=True)
    u.add_argument("--name", required=True)
    u.add_argument("--role", choices=ROLES, default="admin")
    pw = sub.add_parser("passwd", help="set a new password")
    pw.add_argument("email")
    c = sub.add_parser("add-channel", help="add a channel where the bot is an admin")
    c.add_argument("ref", help="@username or -100… id")
    s = sub.add_parser("serve", help="panel and publisher")
    s.add_argument("--no-publisher", action="store_true")
    sub.add_parser("publisher", help="publisher only")
    sub.add_parser("check", help="check the bot token and channel permissions")
    sub.add_parser("stats", help="posts by status")
    return p


def _password(prompt: str) -> str | None:
    password = getpass.getpass(prompt)
    if len(password) < 10:
        print("The password must have at least 10 characters.")
        return None
    return password


async def _add_user(settings: Settings, args) -> int:
    password = _password("Password (10+ characters): ")
    if password is None:
        return 1
    store = await Store.open(settings.database_path)
    try:
        await store.add_user(args.email, args.name, args.role, security.hash_password(password), time.time())
    except Conflict as exc:
        print(exc)
        return 1
    finally:
        await store.close()
    print(f"User {args.email} ({args.role}) created.")
    return 0


async def _passwd(settings: Settings, email: str) -> int:
    password = _password("New password (10+ characters): ")
    if password is None:
        return 1
    store = await Store.open(settings.database_path)
    try:
        found = await store.set_password(email, security.hash_password(password))
    finally:
        await store.close()
    print("Password changed." if found else f"No user {email}.")
    return 0 if found else 1


async def _add_channel(settings: Settings, ref: str) -> int:
    sender = TelegramSender.from_token(settings.bot_token)
    store = await Store.open(settings.database_path)
    try:
        info = await sender.channel(ref)
        await store.add_channel(info.chat_id, info.title, info.username, time.time())
    except ChannelProblem as exc:
        print(exc)
        return 1
    finally:
        await store.close()
        await sender.close()
    print(f"Channel added: {info.title} ({info.chat_id})")
    return 0


async def _serve(settings: Settings, with_publisher: bool) -> int:
    import uvicorn

    from .web import create_app

    store = await Store.open(settings.database_path)
    if not await store.users():
        print("No users yet: create one first with  post-planner add-user --email ... --name ...")
        await store.close()
        return 1
    sender = TelegramSender.from_token(settings.bot_token)
    rewriter = build_rewriter(settings.ai_mode, settings.anthropic_api_key, settings.claude_model)
    server = uvicorn.Server(uvicorn.Config(create_app(store, settings, sender, rewriter), host=settings.host,
                                           port=settings.port, log_level="warning"))
    stop = asyncio.Event()

    async def web():
        await server.serve()
        stop.set()

    tasks = [web()]
    if with_publisher:
        tasks.append(Publisher(store, settings, sender).run(stop))
    print(f"Panel: http://{settings.host}:{settings.port}  (time zone {settings.timezone}, AI: {settings.ai_mode})")
    try:
        await asyncio.gather(*tasks)
    finally:
        await store.close()
        await sender.close()
    return 0


async def _publisher(settings: Settings) -> int:
    store = await Store.open(settings.database_path)
    sender = TelegramSender.from_token(settings.bot_token)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await Publisher(store, settings, sender).run(stop)
    finally:
        await store.close()
        await sender.close()
    return 0


async def _check(settings: Settings) -> int:
    sender = TelegramSender.from_token(settings.bot_token)
    store = await Store.open(settings.database_path)
    ok = True
    try:
        try:
            me = await sender.bot.get_me()
            print(f"✅ Bot: @{me.username}")
        except Exception as exc:  # noqa: BLE001
            print(f"❌ Bot: {classify(exc).message}")
            return 1
        channels = await store.channels()
        if not channels:
            print("⚠️  No channels yet: post-planner add-channel @username")
        for channel in channels:
            try:
                await sender.channel(str(channel.chat_id))
                print(f"✅ {channel.title}: the bot can post")
            except ChannelProblem as exc:
                ok = False
                print(f"❌ {exc}")
    finally:
        await store.close()
        await sender.close()
    return 0 if ok else 1


async def _stats(settings: Settings) -> int:
    store = await Store.open(settings.database_path)
    try:
        counts = await store.counts()
        problems = await store.posts("failed") + await store.posts("unknown")
    finally:
        await store.close()
    print(" · ".join(f"{k}: {v}" for k, v in counts.items() if v))
    for post in problems:
        print(f"  ⚠️  #{post.id} {post.channel_title}: {post.status} — {post.last_error}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = load_settings(need_bot=args.command not in ("add-user", "passwd", "stats"))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    commands = {
        "add-user": lambda: _add_user(settings, args),
        "passwd": lambda: _passwd(settings, args.email),
        "add-channel": lambda: _add_channel(settings, args.ref),
        "serve": lambda: _serve(settings, not args.no_publisher),
        "publisher": lambda: _publisher(settings),
        "check": lambda: _check(settings),
        "stats": lambda: _stats(settings),
    }
    try:
        return asyncio.run(commands[args.command]())
    except KeyboardInterrupt:
        return 130
