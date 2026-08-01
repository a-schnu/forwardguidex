"""Deliver text (e.g. the Morning Brief) to Telegram.

The daily Morning Brief always carries a rates section (UST par yields + NY Fed
EFFR/SOFR), so every outgoing brief must include the Treasury attribution and the
NY Fed disclaimer (R6 (j): NY Fed notice on every channel). The notice text is
version-controlled and pulled verbatim from the `rights` module (legal/*.txt) —
it is NEVER generated or paraphrased here. Use `send_brief(...)` for briefs and
the plain `send_message(...)` for anything without rates.
"""
from __future__ import annotations

from collections.abc import Iterator

import requests

from ..config import get_settings
from . import rights

# Visual divider between the brief body and the appended legal notices.
_NOTICE_DIVIDER = "\n\n—\n"


def _chunks(text: str, size: int = 3800) -> Iterator[str]:
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > size:
            if buf:
                yield buf
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        yield buf


def send_message(text: str) -> bool:
    s = get_settings()
    if not (s.telegram_bot_token and s.telegram_chat_id):
        print("[telegram] token/chat_id not set - skipping send")
        return False
    url = f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage"
    ok = True
    for chunk in _chunks(text):
        r = requests.post(url, data={
            "chat_id": s.telegram_chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }, timeout=60)
        if not r.ok:
            print(f"[telegram] error {r.status_code}: {r.text[:200]}")
            ok = False
    return ok


def rate_notice_block() -> str:
    """Version-controlled Treasury attribution + NY Fed disclaimer for briefs.

    Returns `rights.treasury_attribution()`, a blank line, then
    `rights.nyfed_disclaimer()` — both pulled verbatim from the legal/*.txt files
    via the rights module (never LLM-generated/paraphrased). Degrades gracefully:
    if a legal file is missing or unreadable, whatever notice IS available is
    still returned (and an empty string if neither is), so a send never crashes
    on a missing notice file.
    """
    parts: list[str] = []
    for fn in (rights.treasury_attribution, rights.nyfed_disclaimer):
        try:
            text = (fn() or "").strip()
        except Exception as exc:  # noqa: BLE001 - notice must never break the send
            print(f"[telegram] rate notice unavailable ({fn.__name__}): {exc}")
            continue
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def send_brief(text: str, *, with_rate_notices: bool = True) -> bool:
    """Send a Morning Brief, appending the mandatory rate notices by default.

    When `with_rate_notices` is true (default), `rate_notice_block()` is appended
    to the brief — separated by a divider — before delivery, ensuring the Treasury
    attribution + NY Fed disclaimer accompany the rates section on the Telegram
    channel. If the notice block is empty (files missing), the plain brief is sent.
    """
    body = text
    if with_rate_notices:
        notice = rate_notice_block()
        if notice:
            body = f"{text}{_NOTICE_DIVIDER}{notice}"
    return send_message(body)
