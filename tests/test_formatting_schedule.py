from datetime import datetime

import pytest
from conftest import ROME, local

from post_planner import formatting as f
from post_planner import schedule as s


def test_markup_becomes_telegram_html():
    html = f.to_telegram_html("**Nuovo** _menu_ ~~vecchio~~ `cod_ice` ||sorpresa|| [prenota](https://laurin.example/a_b_c)")
    assert html == ('<b>Nuovo</b> <i>menu</i> <s>vecchio</s> <code>cod_ice</code> <tg-spoiler>sorpresa</tg-spoiler> '
                    '<a href="https://laurin.example/a_b_c">prenota</a>')


def test_html_typed_by_people_is_escaped_and_bad_links_are_not_links():
    html = f.to_telegram_html('<script>alert(1)</script> & [click](javascript:alert(1)) "quotes"')
    assert html == '&lt;script&gt;alert(1)&lt;/script&gt; &amp; [click](javascript:alert(1)) "quotes"'
    assert any("not allowed" in p for p in f.problems("[x](javascript:alert(1))", False))


def test_quotes_and_link_attributes():
    assert f.to_telegram_html("> prima\n> seconda\nfine") == "<blockquote>prima\nseconda</blockquote>\nfine"
    assert f.to_telegram_html("[a](https://x.it/?q=1&b=2)") == '<a href="https://x.it/?q=1&amp;b=2">a</a>'
    # a quote could break out of the href attribute: such a URL is not turned into a link at all
    assert f.to_telegram_html('[a](https://x.it/"onclick=x)') == '[a](https://x.it/"onclick=x)'
    assert f.problems('[a](https://x.it/"onclick=x)', False)


def test_length_is_counted_like_telegram():
    assert f.visible_length(f.to_telegram_html("**ciao** 🍕")) == 7  # the emoji counts as 2
    assert f.problems("a" * 4096, False) == []
    assert f.problems("a" * 4097, False) == ["4097 characters, Telegram allows 4096 for a post"]
    assert f.problems("a" * 1025, True) == ["1025 characters, Telegram allows 1024 for a photo caption"]
    assert f.problems("   ", False) == ["the post is empty"]


def test_buttons():
    buttons, problems = f.parse_buttons("Prenota | https://laurin.example/book\n\nMenu|tg://resolve?domain=x\nbad\n"
                                        "Evil | javascript:alert(1)")
    assert [b.text for b in buttons] == ["Prenota", "Menu"]
    assert problems == ["button line 4: write it as  Text | https://link",
                        "button line 5: links must start with https://, http://, tg:// or mailto:"]


def test_preview_html_shows_line_breaks_and_spoilers():
    assert f.preview_html("a\n<tg-spoiler>b</tg-spoiler>") == 'a<br><span class="spoiler">b</span>'


def test_local_time_and_daylight_saving():
    assert datetime.fromtimestamp(s.parse_local("2026-10-05T09:00", ROME), ROME).hour == 9
    with pytest.raises(s.BadTime, match="does not exist in Europe/Rome"):
        s.parse_local("2026-03-29T02:30", ROME)  # spring forward
    twice = s.parse_local("2026-10-25T02:30", ROME)  # happens twice: the first (summer) one
    assert datetime.fromtimestamp(twice, ROME).utcoffset().total_seconds() == 7200
    with pytest.raises(s.BadTime, match="not a date"):
        s.parse_local("tomorrow", ROME)
    assert s.input_value(local("2026-10-05T09:00"), ROME) == "2026-10-05T09:00"


def test_repeats_keep_the_local_time():
    saturday = local("2026-10-24T09:00")
    sunday = s.next_time(saturday, "daily", ROME)
    assert datetime.fromtimestamp(sunday, ROME).strftime("%a %H:%M") == "Sun 09:00"
    assert sunday - saturday == 25 * 3600  # the clocks went back that night
    friday = local("2026-10-02T09:00")
    assert datetime.fromtimestamp(s.next_time(friday, "weekdays", ROME), ROME).strftime("%a %d") == "Mon 05"
    assert datetime.fromtimestamp(s.next_time(friday, "weekly", ROME), ROME).strftime("%a %d") == "Fri 09"
    assert s.next_time(friday, "", ROME) is None
    night = local("2026-03-28T02:30")  # tomorrow 02:30 does not exist: moved to 03:30
    assert datetime.fromtimestamp(s.next_time(night, "daily", ROME), ROME).strftime("%d %H:%M") == "29 03:30"


def test_week_starts_on_monday():
    days = s.week_days(local("2026-10-01T08:00"), ROME)
    assert [d.strftime("%a %d") for d in (days[0], days[-1])] == ["Mon 28", "Sun 04"]
