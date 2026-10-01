import io
import re

import httpx
import pytest
from aiogram.exceptions import TelegramBadRequest
from conftest import CHAT, METHOD, NOW, make_post
from PIL import Image

from post_planner import security
from post_planner.web import create_app

PASSWORD = "correct horse battery"


class FakeRewriter:
    def __init__(self):
        self.calls = []

    async def rewrite(self, text, action):
        self.calls.append((text, action))
        return f"[{action}] {text}"


@pytest.fixture
async def env(store, settings, sender):
    for email, name, role in (("anna@laurin.it", "Anna", "admin"), ("marco@laurin.it", "Marco", "editor"),
                              ("sara@laurin.it", "Sara", "author")):
        await store.add_user(email, name, role, security.hash_password(PASSWORD), NOW)
    channel = await store.add_channel(CHAT, "Trattoria Laurin", "laurin_news", NOW)
    rewriter = FakeRewriter()
    app = create_app(store, settings, sender, rewriter, clock=lambda: NOW)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://panel") as client:
        yield client, store, channel, rewriter


async def login(client, who="anna"):
    await client.post("/logout", data={"csrf": "x"})
    client.cookies.clear()
    r = await client.post("/login", data={"email": f"{who}@laurin.it", "password": PASSWORD})
    assert r.status_code == 303
    return re.search(r'name="csrf" value="([^"]+)"', (await client.get("/posts/new")).text).group(1)


def draft(csrf, channel, **extra):
    return {"csrf": csrf, "channel_id": str(channel), "text": "**Menu** d'autunno 🍂", "buttons": "",
            "publish_at": "2026-10-05T12:30", "repeat": "", **extra}


async def test_pages_need_login_and_forms_need_csrf(env):
    client, store, channel, _ = env
    for url in ("/calendar", "/posts", "/posts/new", "/channels", "/users", "/media/x.jpg"):
        r = await client.get(url)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), url
    await login(client)
    assert (await client.post("/posts", data=draft("forged", channel))).status_code == 403


async def test_author_writes_editor_returns_then_approves(env):
    client, store, channel, _ = env
    csrf = await login(client, "sara")
    r = await client.post("/posts", data=draft(csrf, channel))
    post_id = int(r.headers["location"].split("/")[2].split("?")[0])
    assert (await client.post(f"/posts/{post_id}/save", data=draft(csrf, channel, action="approve"))).status_code \
        == 403  # authors cannot schedule
    await client.post(f"/posts/{post_id}/save", data=draft(csrf, channel, action="submit"))
    assert (await store.get(post_id)).status == "pending"

    csrf = await login(client, "marco")
    calendar = await client.get("/calendar")
    assert "1 waiting for approval" in calendar.text
    await client.post(f"/posts/{post_id}/reject", data={"csrf": csrf, "note": "add the price"})
    post = await store.get(post_id)
    assert (post.status, post.review_note) == ("draft", "add the price")

    csrf = await login(client, "sara")
    page = await client.get(f"/posts/{post_id}")
    assert "The editor returned it: add the price" in page.text
    await client.post(f"/posts/{post_id}/save", data=draft(csrf, channel, text="**Menu** 25 €", action="submit"))

    csrf = await login(client, "marco")
    r = await client.post(f"/posts/{post_id}/save", data=draft(csrf, channel, text="**Menu** 25 €",
                                                                action="approve"))
    post = await store.get(post_id)
    assert post.status == "scheduled" and post.approved_by is not None
    assert [e["action"] for e in await store.events(post_id)][:3] == ["approved", "edited", "submitted"]


async def test_scheduling_rules(env):
    client, store, channel, _ = env
    csrf = await login(client, "marco")
    r = await client.post("/posts", data=draft(csrf, channel))
    post_id = int(r.headers["location"].split("/")[2].split("?")[0])
    r = await client.post(f"/posts/{post_id}/save", data=draft(csrf, channel, publish_at="2026-09-01T10:00",
                                                                action="approve"))
    assert "Pick a date and time in the future" in (await client.get(r.headers["location"])).text
    r = await client.post(f"/posts/{post_id}/save", data=draft(csrf, channel, publish_at="2027-03-28T02:30",
                                                                action="approve"))
    assert "does not exist in Europe/Rome" in (await client.get(r.headers["location"])).text
    r = await client.post(f"/posts/{post_id}/save", data=draft(csrf, channel, text="x" * 4100, action="approve"))
    assert "Telegram allows 4096" in (await client.get(r.headers["location"])).text
    assert (await store.get(post_id)).status == "draft"
    await client.post(f"/posts/{post_id}/save", data=draft(csrf, channel, action="publish_now"))
    post = await store.get(post_id)
    assert post.status == "scheduled" and post.publish_at == NOW


async def test_preview_escapes_and_calendar_places_posts_by_local_day(env):
    client, store, channel, _ = env
    csrf = await login(client)
    r = await client.post("/posts", data=draft(csrf, channel, text="<script>x</script> **ok**"))
    page = await client.get(r.headers["location"])
    assert "&lt;script&gt;x&lt;/script&gt; <b>ok</b>" in page.text and "<script>x" not in page.text
    await make_post(store, channel, when=NOW + 3 * 3600, text="Pranzo alle 11")
    calendar = (await client.get("/calendar")).text
    thursday = calendar.split("Thu <span>01/10</span>")[1].split("</section>")[0]
    assert "Pranzo alle 11" in thursday and "11:00" in thursday


async def test_photo_is_cleaned_and_junk_refused(env, settings):
    client, store, channel, _ = env
    csrf = await login(client)
    img = Image.new("RGB", (40, 30), "orange")
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"  # camera maker; GPS would be stripped the same way
    raw = io.BytesIO()
    img.save(raw, "JPEG", exif=exif)
    r = await client.post("/posts", data=draft(csrf, channel), files={"photo": ("p.jpg", raw.getvalue(), "image/jpeg")})
    post = await store.get(int(r.headers["location"].split("/")[2].split("?")[0]))
    saved = settings.media_dir / post.photo
    assert saved.exists() and b"SecretCam" not in saved.read_bytes()
    assert (await client.get(f"/media/{post.photo}")).headers["content-type"] == "image/jpeg"
    assert (await client.get("/media/..%2F..%2Fetc%2Fpasswd")).status_code == 404
    r = await client.post("/posts", data=draft(csrf, channel), files={"photo": ("p.jpg", b"not an image", "x")})
    assert "the photo is damaged" in (await client.get(r.headers["location"])).text


async def test_unknown_outcome_is_resolved_by_a_person(env):
    client, store, channel, _ = env
    csrf = await login(client, "marco")
    first = await make_post(store, channel, status="unknown")
    second = await make_post(store, channel, status="unknown")
    await client.post(f"/posts/{first}/mark_published", data={"csrf": csrf})
    await client.post(f"/posts/{second}/resend", data={"csrf": csrf})
    assert (await store.get(first)).status == "published"
    post = await store.get(second)
    assert post.status == "scheduled" and post.publish_at == NOW
    failed = await make_post(store, channel, status="failed")
    await client.post(f"/posts/{failed}/retry", data={"csrf": csrf})
    assert (await store.get(failed)).status == "scheduled"


async def test_published_post_is_edited_and_deleted_in_the_channel(env, sender):
    client, store, channel, _ = env
    csrf = await login(client, "marco")
    post_id = await make_post(store, channel, status="published")
    await store.published(post_id, 555, NOW, None)
    await client.post(f"/posts/{post_id}/save", data={"csrf": csrf, "channel_id": str(channel),
                                                       "text": "**Menu** aggiornato", "buttons": "", "action": "save"})
    assert sender.edited == [{"message_id": 555, "html": "<b>Menu</b> aggiornato", "buttons": []}]
    assert (await store.get(post_id)).text == "**Menu** aggiornato"
    sender.fail = [TelegramBadRequest(METHOD, "Bad Request: message is not modified")]
    r = await client.post(f"/posts/{post_id}/save", data={"csrf": csrf, "channel_id": str(channel),
                                                           "text": "**Menu** aggiornato", "action": "save"})
    assert "message is not modified" in (await client.get(r.headers["location"])).text
    await client.post(f"/posts/{post_id}/delete", data={"csrf": csrf})
    assert sender.deleted == [555] and (await store.get(post_id)).status == "deleted"


async def test_ai_suggestion_and_daily_limit(env, settings):
    client, store, channel, rewriter = env
    csrf = await login(client)
    r = await client.post("/posts", data=draft(csrf, channel))
    post_id = int(r.headers["location"].split("/")[2].split("?")[0])
    page = await client.post(f"/posts/{post_id}/ai/it", data=draft(csrf, channel, text="Nuovo menu"))
    assert "AI suggestion · → Italian" in page.text and "[it] Nuovo menu" in page.text
    assert (await store.get(post_id)).text != "[it] Nuovo menu"  # only a suggestion
    for _ in range(settings.ai_daily_limit):
        await store.take_ai_call("2026-10-01", settings.ai_daily_limit)
    r = await client.post(f"/posts/{post_id}/ai/it", data=draft(csrf, channel))
    assert "Daily AI limit" in (await client.get(r.headers["location"])).text


async def test_channels_and_users_are_for_admins(env):
    client, store, *_ = env
    csrf = await login(client, "marco")
    assert (await client.get("/channels")).status_code == 403
    csrf = await login(client)
    r = await client.post("/channels", data={"csrf": csrf, "ref": "@unknown"})
    assert "add the bot as an administrator first" in (await client.get(r.headers["location"])).text
    r = await client.post("/channels", data={"csrf": csrf, "ref": "@laurin_news"})
    assert "Trattoria Laurin added." in (await client.get(r.headers["location"])).text
    await client.post("/users", data={"csrf": csrf, "email": "leo@laurin.it", "name": "Leo", "role": "author",
                                      "password": "long enough pw"})
    assert (await store.user_by_email("leo@laurin.it")).role == "author"


async def test_login_limits_and_headers(env):
    client, *_ = env
    for _ in range(5):
        await client.post("/login", data={"email": "anna@laurin.it", "password": "nope"})
    r = await client.post("/login", data={"email": "anna@laurin.it", "password": PASSWORD})
    assert "Too many attempts" in r.text
    r = await client.get("/health")
    assert r.json() == {"status": "ok", "scheduled": 0, "problems": 0}
    assert "default-src 'self'" in r.headers["content-security-policy"]
