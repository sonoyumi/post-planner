"""Post text: simple markup typed by people -> Telegram HTML, plus length rules and a preview.

Authors write:  **bold**  _italic_  ~~strike~~  `code`  ||spoiler||  [text](https://link)  and "> " for a quote.
Everything else is escaped first, so a "<script>" or a stray "<" is just text — in Telegram and in the panel.
Links are allowed only to http(s), tg:// and mailto: (no javascript:).

Telegram counts length in UTF-16 code units of the visible text (an emoji counts as 2): a post may have
4096 of them, a photo caption 1024.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
MAX_BUTTONS = 6

_SAFE_URL = re.compile(r"^(https?://[^\s<>\"']+|tg://[^\s<>\"']+|mailto:[^\s<>\"']+)$", re.IGNORECASE)
_CODE = re.compile(r"`([^`\n]+)`")
_INLINE = [
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S), r"<b>\1</b>"),
    (re.compile(r"(?<![\w*])_(?=\S)(.+?)(?<=\S)_(?!\w)", re.S), r"<i>\1</i>"),
    (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S), r"<s>\1</s>"),
    (re.compile(r"\|\|(?=\S)(.+?)(?<=\S)\|\|", re.S), r"<tg-spoiler>\1</tg-spoiler>"),
]
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class Button:
    text: str
    url: str


def safe_url(url: str) -> bool:
    return bool(_SAFE_URL.match(url.strip()))


def _link(m: re.Match) -> str:
    text, url = m.group(1), html.unescape(m.group(2))
    if not safe_url(url):
        return m.group(0)  # left as typed: the checks report it
    return f'<a href="{html.escape(url, quote=True)}">{text}</a>'


def to_telegram_html(source: str) -> str:
    """Markup -> HTML that Telegram accepts (parse_mode=HTML)."""
    out, quote = [], []

    def flush():
        if quote:
            out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            quote.clear()

    for line in html.escape(source.replace("\r\n", "\n").strip(), quote=False).split("\n"):
        if line.startswith("&gt; ") or line == "&gt;":
            quote.append(line[5:])
            continue
        flush()
        out.append(line)
    flush()
    text = "\n".join(out)
    # Code spans and links are set aside first: "_" inside a URL or in code must not become italic.
    kept: list[str] = []

    def keep(fragment: str) -> str:
        kept.append(fragment)
        return f"\x00{len(kept) - 1}\x00"

    text = _CODE.sub(lambda m: keep(f"<code>{m.group(1)}</code>"), text)
    text = _LINK.sub(lambda m: keep(_link(m)), text)
    for pattern, repl in _INLINE:
        text = pattern.sub(repl, text)
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], text)


def visible_length(telegram_html: str) -> int:
    """Length as Telegram counts it: UTF-16 code units of the text without tags."""
    plain = html.unescape(_TAG.sub("", telegram_html))
    return len(plain.encode("utf-16-le")) // 2


def preview_html(telegram_html: str) -> str:
    """The same HTML for the panel: line breaks shown, spoilers styled. Built only from escaped input + our tags."""
    return (telegram_html.replace("<tg-spoiler>", '<span class="spoiler">').replace("</tg-spoiler>", "</span>")
            .replace("\n", "<br>"))


def parse_buttons(raw: str) -> tuple[list[Button], list[str]]:
    """One button per line: `Text | https://link`."""
    buttons, problems = [], []
    for n, line in enumerate((x.strip() for x in raw.splitlines()), 1):
        if not line:
            continue
        text, sep, url = (part.strip() for part in line.partition("|"))
        if not sep or not text or not url:
            problems.append(f"button line {n}: write it as  Text | https://link")
        elif not safe_url(url):
            problems.append(f"button line {n}: links must start with https://, http://, tg:// or mailto:")
        elif len(text) > 64:
            problems.append(f"button line {n}: the text is longer than 64 characters")
        else:
            buttons.append(Button(text, url))
    if len(buttons) > MAX_BUTTONS:
        problems.append(f"at most {MAX_BUTTONS} buttons")
        buttons = buttons[:MAX_BUTTONS]
    return buttons, problems


def problems(source: str, has_photo: bool, buttons_raw: str = "") -> list[str]:
    """Everything that would make Telegram reject the post (or the reader frown)."""
    found: list[str] = []
    text = to_telegram_html(source)
    length = visible_length(text)
    limit = CAPTION_LIMIT if has_photo else TEXT_LIMIT
    if not source.strip() and not has_photo:
        found.append("the post is empty")
    if length > limit:
        what = "a photo caption" if has_photo else "a post"
        found.append(f"{length} characters, Telegram allows {limit} for {what}")
    for m in _LINK.finditer(html.escape(source, quote=False)):
        if not safe_url(html.unescape(m.group(2))):
            found.append(f"link “{html.unescape(m.group(2))}” is not allowed (only https://, http://, tg://, mailto:)")
    found += parse_buttons(buttons_raw)[1]
    return found
