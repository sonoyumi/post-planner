from datetime import datetime

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from conftest import CHAT, METHOD, NOW, ROME, local, make_post

from post_planner.publisher import MAX_ATTEMPTS, RETRY_BASE, Publisher
from post_planner.store import LEASE


async def setup(store, settings, sender, now=NOW):
    channel = await store.add_channel(CHAT, "Trattoria Laurin", "laurin_news", now)
    clock = [now]
    return channel, Publisher(store, settings, sender, clock=lambda: clock[0]), clock


async def test_due_post_is_published_and_pinned(store, settings, sender):
    channel, pub, _ = await setup(store, settings, sender)
    post_id = await make_post(store, channel, pin=1, silent=1, buttons="Prenota | https://laurin.example/book")
    later = await make_post(store, channel, when=NOW + 3600)
    assert await pub.tick() is True
    assert await pub.tick() is False  # the other one is not due yet
    [sent] = sender.sent
    assert sent["html"] == "<b>Menu</b> del giorno" and sent["silent"] is True
    assert [b.url for b in sent["buttons"]] == ["https://laurin.example/book"]
    post = await store.get(post_id)
    assert (post.status, post.message_id) == ("published", sent["message_id"])
    assert sender.pinned == [sent["message_id"]]
    assert (await store.get(later)).status == "scheduled"


async def test_repeating_post_schedules_its_next_run_at_the_same_local_time(store, settings, sender):
    saturday = local("2026-10-24T09:00")
    channel, pub, _ = await setup(store, settings, sender, now=saturday + 5)
    post_id = await make_post(store, channel, when=saturday, repeat="daily")
    await pub.tick()
    copies = [p for p in await store.posts("scheduled") if p.series_id == post_id]
    assert len(copies) == 1
    assert datetime.fromtimestamp(copies[0].publish_at, ROME).strftime("%a %d %H:%M") == "Sun 25 09:00"


async def test_flood_control_waits_exactly_as_long_as_telegram_asks(store, settings, sender):
    channel, pub, clock = await setup(store, settings, sender)
    post_id = await make_post(store, channel)
    sender.fail = [TelegramRetryAfter(METHOD, "Too Many Requests", retry_after=17)]
    await pub.tick()
    post = await store.get(post_id)
    assert post.status == "scheduled" and "wait 17 s" in post.last_error
    clock[0] += 16
    assert await pub.tick() is False
    clock[0] += 2
    assert await pub.tick() is True and (await store.get(post_id)).status == "published"


async def test_network_errors_back_off_then_give_up(store, settings, sender):
    channel, pub, clock = await setup(store, settings, sender)
    post_id = await make_post(store, channel)
    sender.fail = [TelegramNetworkError(METHOD, "timeout") for _ in range(MAX_ATTEMPTS)]
    for attempt in range(1, MAX_ATTEMPTS + 1):
        assert await pub.tick() is True
        clock[0] += RETRY_BASE * 2 ** (attempt - 1) + 1
    post = await store.get(post_id)
    assert post.status == "failed" and post.attempts == MAX_ATTEMPTS and sender.sent == []


async def test_permanent_errors_fail_at_once(store, settings, sender):
    channel, pub, _ = await setup(store, settings, sender)
    kicked = await make_post(store, channel)
    too_long = await make_post(store, channel)
    sender.fail = [TelegramForbiddenError(METHOD, "bot is not a member of the channel chat"),
                   TelegramBadRequest(METHOD, "Bad Request: message is too long")]
    await pub.tick()
    await pub.tick()
    assert (await store.get(kicked)).last_error.startswith("the bot is not an administrator of this channel any more")
    assert (await store.get(too_long)).last_error == "Telegram refused: Bad Request: message is too long"
    assert {(await store.get(i)).status for i in (kicked, too_long)} == {"failed"}


async def test_a_crash_while_sending_never_sends_twice(store, settings, sender):
    channel, pub, clock = await setup(store, settings, sender)
    post_id = await make_post(store, channel)
    await store.claim(NOW)  # a publisher took it and died before saving the result
    clock[0] += LEASE + 1
    assert await pub.tick() is False
    post = await store.get(post_id)
    assert post.status == "unknown" and "check the channel" in post.last_error
    assert sender.sent == []  # a person decides: mark published or send again


async def test_missing_photo_and_failed_pin(store, settings, sender):
    channel, pub, _ = await setup(store, settings, sender)
    no_file = await make_post(store, channel, photo="gone.jpg")
    pinned = await make_post(store, channel, pin=1)
    sender.pin_fail = TelegramBadRequest(METHOD, "not enough rights to pin a message")
    await pub.tick()
    await pub.tick()
    assert (await store.get(no_file)).last_error == "the photo file is missing"
    assert (await store.get(pinned)).status == "published"  # the post is out; pinning is a bonus
