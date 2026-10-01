"""The team's panel (FastAPI + Jinja2): calendar, posts, editor with a Telegram-like preview, approval.

Roles: an author writes drafts and sends them for approval; an editor approves, schedules, publishes and
fixes; an admin also manages channels and users. Every form carries a CSRF token, every page needs a
signed session cookie, post text is shown through our own escaped HTML only.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps
from starlette.datastructures import UploadFile

from . import formatting, schedule, security
from .ai import ACTIONS, AIError, Rewriter
from .config import Settings
from .store import ROLES, STATUSES, Conflict, NotFound, Post, Store, User
from .telegram import ChannelProblem, Sender, classify

HERE = Path(__file__).parent
COOKIE = "pp_session"
LOGIN_WINDOW, LOGIN_MAX_FAILS = 300, 5
PHOTO_MAX_MB = 10
STATUS_LABELS = {"draft": "Draft", "pending": "Waiting for approval", "scheduled": "Scheduled", "sending": "Sending",
                 "published": "Published", "failed": "Failed", "unknown": "Check the channel", "deleted": "Deleted"}
AI_LABELS = {"shorter": "Shorter", "friendlier": "Friendlier", "formal": "More formal", "fix": "Fix spelling",
             "it": "→ Italian", "en": "→ English", "de": "→ German", "uk": "→ Ukrainian"}


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    pass


class LoginLimiter:
    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.fails: dict[str, deque[float]] = defaultdict(deque)

    def blocked(self, ip: str) -> bool:
        q = self.fails[ip]
        while q and q[0] < self.clock() - LOGIN_WINDOW:
            q.popleft()
        return len(q) >= LOGIN_MAX_FAILS

    def fail(self, ip: str) -> None:
        self.fails[ip].append(self.clock())

    def reset(self, ip: str) -> None:
        self.fails.pop(ip, None)


def save_photo(data: bytes, media_dir: Path) -> str:
    """Any JPEG/PNG/WEBP -> a clean JPEG named by its hash (EXIF with GPS and the camera is dropped)."""
    if len(data) > PHOTO_MAX_MB * 1024 * 1024:
        raise ValueError(f"the photo is larger than {PHOTO_MAX_MB} MB")
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format not in ("JPEG", "PNG", "WEBP"):
                raise ValueError("photos must be JPG, PNG or WEBP")
            img = ImageOps.exif_transpose(img).convert("RGB")
            if max(img.size) > 2560:
                img.thumbnail((2560, 2560))
            out = io.BytesIO()
            img.save(out, "JPEG", quality=88)
    except (OSError, Image.DecompressionBombError, SyntaxError):
        raise ValueError("the photo is damaged or too large") from None
    jpeg = out.getvalue()
    name = hashlib.sha256(jpeg).hexdigest()[:32] + ".jpg"
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / name).write_bytes(jpeg)
    return name


def create_app(store: Store, settings: Settings, sender: Sender | None = None, rewriter: Rewriter | None = None,
               clock: Callable[[], float] = time.time) -> FastAPI:
    app = FastAPI(title="Post planner", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    tz = settings.tz
    templates.env.globals.update(STATUS=STATUS_LABELS, REPEATS=schedule.REPEATS, AI=AI_LABELS, team=settings.team_name,
                                 ai_on=rewriter is not None, tzname=settings.timezone)
    templates.env.filters.update(
        when=lambda ts: schedule.to_local(ts, tz).strftime("%a %d/%m %H:%M") if ts else "—",
        hm=lambda ts: schedule.to_local(ts, tz).strftime("%H:%M") if ts else "",
        tg=lambda text: formatting.preview_html(formatting.to_telegram_html(text)))
    limiter = LoginLimiter(clock)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; form-action 'self'")
        return response

    @app.exception_handler(LoginRequired)
    async def to_login(request: Request, exc: LoginRequired):
        return RedirectResponse("/login?next=" + quote(request.url.path), status_code=303)

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc: Forbidden):
        return HTMLResponse("Forbidden", status_code=403)

    @app.exception_handler(NotFound)
    async def not_found(request: Request, exc: NotFound):
        return HTMLResponse("Not found", status_code=404)

    async def current_user(request: Request) -> User:
        user_id = security.read_session(settings.secret_key, request.cookies.get(COOKIE), clock())
        user = await store.user(user_id) if user_id else None
        if user is None:
            raise LoginRequired
        return user

    async def checked_form(request: Request):
        form = await request.form()
        if not security.check_csrf(settings.secret_key, request.cookies.get(COOKIE), form.get("csrf")):
            raise Forbidden
        return form

    def page(request: Request, name: str, user: User | None, **ctx) -> HTMLResponse:
        cookie = request.cookies.get(COOKIE) or ""
        return templates.TemplateResponse(request, name, {
            "user": user, "csrf": security.csrf_token(settings.secret_key, cookie) if cookie else "", **ctx})

    def back(post_id: int, flash: str) -> RedirectResponse:
        return RedirectResponse(f"/posts/{post_id}?flash=" + quote(flash), status_code=303)

    def can_edit(user: User, post: Post) -> bool:
        if post.status not in ("draft", "pending", "scheduled", "failed"):
            return False
        return user.can_publish or (post.author_id == user.id and post.status in ("draft", "pending"))

    # ------------------------------------------------------------ login
    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, next: str = "/calendar"):
        return page(request, "login.html", None, next=next, error="")

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        ip = request.client.host if request.client else "?"
        nxt = str(form.get("next") or "/calendar")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/calendar"
        if limiter.blocked(ip):
            return page(request, "login.html", None, next=nxt, error="Too many attempts, wait 5 minutes.")
        user = await store.user_by_email(str(form.get("email", "")))
        if user is None or not security.verify_password(str(form.get("password", "")), user.password_hash):
            limiter.fail(ip)
            return page(request, "login.html", None, next=nxt, error="Wrong e-mail or password.")
        limiter.reset(ip)
        response = RedirectResponse(nxt, status_code=303)
        response.set_cookie(COOKIE, security.make_session(settings.secret_key, user.id, clock()), httponly=True,
                            samesite="lax", secure=settings.cookie_secure, max_age=security.SESSION_TTL)
        return response

    @app.post("/logout")
    async def logout(request: Request):
        await checked_form(request)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    # ------------------------------------------------------------ calendar and lists
    @app.get("/")
    async def root():
        return RedirectResponse("/calendar", status_code=303)

    @app.get("/calendar", response_class=HTMLResponse)
    async def calendar(request: Request, week: str = ""):
        user = await current_user(request)
        try:
            anchor = schedule.parse_local(week + "T12:00", tz) if week else clock()
        except schedule.BadTime:
            anchor = clock()
        days = schedule.week_days(anchor, tz)
        start, end = days[0].timestamp(), (days[-1] + timedelta(days=1)).timestamp()
        posts = await store.posts(since=start, until=end)
        by_day: dict[str, list[Post]] = defaultdict(list)
        for post in sorted(posts, key=lambda p: p.published_at or p.publish_at or 0):
            ts = post.published_at or post.publish_at
            if ts and post.status != "deleted":
                by_day[schedule.to_local(ts, tz).strftime("%Y-%m-%d")].append(post)
        counts = await store.counts()
        today = schedule.to_local(clock(), tz).strftime("%Y-%m-%d")
        return page(request, "calendar.html", user, days=days, by_day=by_day, counts=counts, today=today,
                    prev=(days[0] - timedelta(days=7)).strftime("%Y-%m-%d"),
                    next=(days[0] + timedelta(days=7)).strftime("%Y-%m-%d"),
                    channels=await store.channels(), active="calendar")

    @app.get("/posts", response_class=HTMLResponse)
    async def posts(request: Request, status: str = "", channel: int = 0, mine: int = 0):
        user = await current_user(request)
        status = status if status in STATUSES else ""
        found = await store.posts(status or None, channel or None, user.id if mine else None)
        return page(request, "posts.html", user, posts=found, counts=await store.counts(), status=status,
                    channel=channel, mine=mine, channels=await store.channels(), active="posts")

    # ------------------------------------------------------------ one post
    def form_fields(form, post: Post | None) -> tuple[dict, list[str]]:
        problems: list[str] = []
        try:
            channel_id = int(form.get("channel_id", "0"))
        except ValueError:
            channel_id = 0
        when = str(form.get("publish_at", "")).strip()
        publish_at = None
        if when:
            try:
                publish_at = schedule.parse_local(when, tz)
            except schedule.BadTime as exc:
                problems.append(str(exc))
                publish_at = post.publish_at if post else None
        repeat = str(form.get("repeat", ""))
        fields = {
            "channel_id": channel_id,
            "text": str(form.get("text", ""))[:10000].replace("\r\n", "\n"),
            "photo": post.photo if post else "",
            "buttons": str(form.get("buttons", ""))[:2000],
            "repeat": repeat if repeat in schedule.REPEATS else "",
            "pin": int(form.get("pin") == "1"),
            "silent": int(form.get("silent") == "1"),
            "publish_at": publish_at,
        }
        if form.get("remove_photo") == "1":
            fields["photo"] = ""
        return fields, problems

    async def apply_photo(form, fields: dict) -> str | None:
        photo = form.get("photo")
        if isinstance(photo, UploadFile) and photo.filename:
            data = await photo.read(PHOTO_MAX_MB * 1024 * 1024 + 1)
            try:
                fields["photo"] = await asyncio.to_thread(save_photo, data, settings.media_dir)
            except ValueError as exc:
                return str(exc)
        return None

    @app.get("/posts/new", response_class=HTMLResponse)
    async def new_post(request: Request):
        user = await current_user(request)
        channels = await store.channels()
        return page(request, "post.html", user, post=None, channels=channels, editable=True, events=[],
                    problems=[], flash="" if channels else "Add a channel first (admin → Channels).",
                    when_value="", suggestion="", active="posts")

    @app.post("/posts")
    async def create(request: Request):
        user = await current_user(request)
        form = await checked_form(request)
        fields, problems = form_fields(form, None)
        await store.channel(fields["channel_id"])  # 404 for an unknown channel
        error = await apply_photo(form, fields)
        post_id = await store.create_post(user.id, fields, clock())
        flash = " · ".join([*problems, error] if error else problems) or "Draft saved."
        return back(post_id, flash)

    @app.get("/posts/{post_id}", response_class=HTMLResponse)
    async def show(request: Request, post_id: int, flash: str = ""):
        user = await current_user(request)
        post = await store.get(post_id)
        return page(request, "post.html", user, post=post, channels=await store.channels(),
                    editable=can_edit(user, post) or (post.status == "published" and user.can_publish),
                    problems=formatting.problems(post.text, bool(post.photo), post.buttons),
                    events=await store.events(post_id), flash=flash, suggestion="",
                    when_value=schedule.input_value(post.publish_at, tz), active="posts")

    @app.post("/posts/{post_id}/save")
    async def save(request: Request, post_id: int):
        user = await current_user(request)
        form = await checked_form(request)
        post = await store.get(post_id)
        action = str(form.get("action", "save"))
        if post.status == "published":
            return await update_live(user, post, form)
        if not can_edit(user, post):
            raise Forbidden
        fields, problems = form_fields(form, post)
        await store.channel(fields["channel_id"])
        error = await apply_photo(form, fields)
        if error:
            problems.append(error)
        await store.update_post(post_id, user.id, fields, clock())
        problems += formatting.problems(fields["text"], bool(fields["photo"]), fields["buttons"])
        if action == "save" or problems:
            return back(post_id, " · ".join(problems) if problems else "Saved.")
        now = clock()
        try:
            if action == "submit":
                await store.move(post_id, user.id, ("draft",), "pending", "submitted", now)
                return back(post_id, "Sent to the editor for approval.")
            if not user.can_publish:
                raise Forbidden
            if action == "publish_now":
                await store.move(post_id, user.id, ("draft", "pending", "scheduled", "failed"), "scheduled",
                                 "approved", now, "publish now", publish_at=now, approved_by=user.id, next_try_at=0)
                return back(post_id, "Publishing in a few seconds.")
            if action == "approve":
                if not fields["publish_at"] or fields["publish_at"] <= now:
                    return back(post_id, "Pick a date and time in the future, or use “Publish now”.")
                await store.move(post_id, user.id, ("draft", "pending", "scheduled", "failed"), "scheduled",
                                 "approved", now, f"for {schedule.to_local(fields['publish_at'], tz):%d/%m %H:%M}",
                                 approved_by=user.id, next_try_at=0)
                return back(post_id, "Approved and scheduled.")
        except Conflict as exc:
            return back(post_id, str(exc))
        return back(post_id, "Saved.")

    async def update_live(user: User, post: Post, form) -> Response:
        """A published post: change the text and buttons in the channel itself."""
        if not user.can_publish or sender is None:
            raise Forbidden
        text, buttons_raw = str(form.get("text", ""))[:10000].replace("\r\n", "\n"), str(form.get("buttons", ""))
        problems = formatting.problems(text, bool(post.photo), buttons_raw)
        if problems:
            return back(post.id, " · ".join(problems))
        channel = await store.channel(post.channel_id)
        buttons, _ = formatting.parse_buttons(buttons_raw)
        try:
            await sender.edit(channel.chat_id, post.message_id or 0, formatting.to_telegram_html(text),
                              bool(post.photo), buttons)
        except Exception as exc:  # noqa: BLE001
            return back(post.id, "Telegram did not accept the change: " + classify(exc).message)
        await store.update_text(post.id, user.id, text, buttons_raw, clock())
        return back(post.id, "Updated in the channel.")

    @app.post("/posts/{post_id}/reject")
    async def reject(request: Request, post_id: int):
        user = await current_user(request)
        form = await checked_form(request)
        if not user.can_publish:
            raise Forbidden
        note = str(form.get("note", "")).strip()[:500] or "needs changes"
        try:
            await store.move(post_id, user.id, ("pending",), "draft", "returned", clock(), note, review_note=note)
        except Conflict as exc:
            return back(post_id, str(exc))
        return back(post_id, "Returned to the author.")

    @app.post("/posts/{post_id}/{verb}")
    async def simple_action(request: Request, post_id: int, verb: str):
        user = await current_user(request)
        await checked_form(request)
        if not user.can_publish:
            raise Forbidden
        now = clock()
        moves = {
            "cancel": (("scheduled",), "draft", "cancelled", {}),
            "retry": (("failed",), "scheduled", "retry", {"publish_at": now, "next_try_at": 0, "last_error": ""}),
            "resend": (("unknown",), "scheduled", "send again", {"publish_at": now, "next_try_at": 0,
                                                                  "last_error": ""}),
        }
        try:
            if verb in moves:
                allowed, status, action, extra = moves[verb]
                await store.move(post_id, user.id, allowed, status, action, now, **extra)
                return back(post_id, {"cancel": "Back to draft.", "retry": "Will be sent again in a few seconds.",
                                      "resend": "Will be sent again in a few seconds."}[verb])
            if verb == "mark_published":
                await store.move(post_id, user.id, ("unknown",), "published", "marked published", now,
                                 "checked the channel by hand", last_error="")
                return back(post_id, "Marked as published.")
            if verb == "delete" and sender is not None:
                post = await store.get(post_id)
                channel = await store.channel(post.channel_id)
                if post.status != "published" or not post.message_id:
                    return back(post_id, "Only a published post can be deleted from the channel.")
                try:
                    await sender.delete(channel.chat_id, post.message_id)
                except Exception as exc:  # noqa: BLE001
                    return back(post_id, "Telegram did not delete it: " + classify(exc).message)
                await store.move(post_id, user.id, ("published",), "deleted", "deleted from channel", now)
                return back(post_id, "Deleted from the channel.")
        except Conflict as exc:
            return back(post_id, str(exc))
        raise NotFound(verb)

    @app.post("/posts/{post_id}/ai/{action}", response_class=HTMLResponse)
    async def ai_rewrite(request: Request, post_id: int, action: str):
        user = await current_user(request)
        form = await checked_form(request)
        post = await store.get(post_id)
        if rewriter is None or action not in ACTIONS or not can_edit(user, post):
            raise Forbidden
        text = str(form.get("text", "")) or post.text
        day = datetime.fromtimestamp(clock(), UTC).strftime("%Y-%m-%d")
        if not await store.take_ai_call(day, settings.ai_daily_limit):
            return back(post_id, f"Daily AI limit ({settings.ai_daily_limit}) reached.")
        try:
            suggestion = await rewriter.rewrite(text, action)
        except AIError as exc:
            return back(post_id, str(exc))
        return page(request, "post.html", user, post=post, channels=await store.channels(), editable=True,
                    problems=formatting.problems(post.text, bool(post.photo), post.buttons),
                    events=await store.events(post_id), flash="", suggestion=suggestion, ai_action=action,
                    when_value=schedule.input_value(post.publish_at, tz), active="posts")

    @app.get("/media/{name}")
    async def media(request: Request, name: str):
        await current_user(request)
        if not name.endswith(".jpg") or "/" in name or ".." in name:
            raise NotFound(name)
        path = settings.media_dir / name
        if not path.is_file():
            raise NotFound(name)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    # ------------------------------------------------------------ admin
    @app.get("/channels", response_class=HTMLResponse)
    async def channels(request: Request, flash: str = ""):
        user = await current_user(request)
        if user.role != "admin":
            raise Forbidden
        return page(request, "channels.html", user, channels=await store.channels(), flash=flash, active="channels")

    @app.post("/channels")
    async def add_channel(request: Request):
        user = await current_user(request)
        form = await checked_form(request)
        if user.role != "admin" or sender is None:
            raise Forbidden
        try:
            info = await sender.channel(str(form.get("ref", "")))
        except ChannelProblem as exc:
            return RedirectResponse("/channels?flash=" + quote(str(exc)), status_code=303)
        await store.add_channel(info.chat_id, info.title, info.username, clock())
        return RedirectResponse("/channels?flash=" + quote(f"{info.title} added."), status_code=303)

    @app.get("/users", response_class=HTMLResponse)
    async def users(request: Request, flash: str = ""):
        user = await current_user(request)
        if user.role != "admin":
            raise Forbidden
        return page(request, "users.html", user, users=await store.users(), roles=ROLES, flash=flash, active="users")

    @app.post("/users")
    async def add_user(request: Request):
        user = await current_user(request)
        form = await checked_form(request)
        if user.role != "admin":
            raise Forbidden
        email, name = str(form.get("email", "")).strip(), str(form.get("name", "")).strip()
        password, role = str(form.get("password", "")), str(form.get("role", "author"))
        if "@" not in email or not name or len(password) < 10 or role not in ROLES:
            return RedirectResponse("/users?flash=" + quote("E-mail, name and a password of 10+ characters needed."),
                                    status_code=303)
        try:
            await store.add_user(email, name, role, security.hash_password(password), clock())
        except Conflict as exc:
            return RedirectResponse("/users?flash=" + quote(str(exc)), status_code=303)
        return RedirectResponse("/users?flash=" + quote(f"{email} added."), status_code=303)

    @app.get("/health")
    async def health():
        counts = await store.counts()
        return JSONResponse({"status": "ok", "scheduled": counts["scheduled"],
                             "problems": counts["failed"] + counts["unknown"]})

    return app
