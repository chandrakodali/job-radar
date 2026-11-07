"""Phone notifications through Telegram and/or ntfy. Both are optional and free."""
from __future__ import annotations

import html
import logging
import os
import time

import requests

from .http import USER_AGENT

log = logging.getLogger("radar.notify")


def _fmt_age(hours: float | None) -> str:
    if hours is None:
        return "date unknown"
    if hours < 1:
        return "posted <1h ago"
    if hours < 48:
        return f"posted {int(hours)}h ago"
    return f"posted {int(hours // 24)}d ago"


def format_job_html(m: dict) -> str:
    e = html.escape
    lines = [
        f"<b>{e(m['title'])}</b>",
        f"{e(m['company'])} · {e(m['location'] or 'location n/a')}{(' · ' + e(m['workplace'])) if m.get('workplace') else ''}",
        f"Score {m['score']} · {_fmt_age(m.get('age_hours'))}",
    ]
    if m.get("flags"):
        lines.append(e(" · ".join(m["flags"])))
    lines.append(f'<a href="{e(m["url"], quote=True)}">Open job posting</a>')
    return "\n".join(lines)


def format_job_text(m: dict) -> str:
    parts = [
        f"{m['company']} · {m['location'] or 'location n/a'}" + (f" · {m['workplace']}" if m.get("workplace") else ""),
        f"Score {m['score']} · {_fmt_age(m.get('age_hours'))}",
    ]
    if m.get("flags"):
        parts.append(" · ".join(m["flags"]))
    return "\n".join(parts)


class Notifier:
    def __init__(self, state: dict, dry_run: bool = False):
        self.state = state
        self.dry_run = dry_run
        self.tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or str(state.get("telegram_chat_id") or "")
        self.ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()
        self.ntfy_server = (os.environ.get("NTFY_SERVER", "").strip() or "https://ntfy.sh").rstrip("/")
        self.sent = 0
        if self.tg_token and not self.tg_chat:
            self._discover_telegram_chat()

    @property
    def configured(self) -> bool:
        return bool((self.tg_token and self.tg_chat) or self.ntfy_topic)

    def describe(self) -> str:
        ch = []
        if self.tg_token:
            ch.append("telegram" + ("" if self.tg_chat else " (waiting: send /start to your bot)"))
        if self.ntfy_topic:
            ch.append("ntfy")
        return ", ".join(ch) or "none"

    # -- Telegram
    def _discover_telegram_chat(self) -> None:
        """Find the chat id from the latest message sent to the bot (user sends /start once)."""
        try:
            r = requests.get(f"https://api.telegram.org/bot{self.tg_token}/getUpdates", timeout=20)
            data = r.json()
            for upd in reversed(data.get("result") or []):
                msg = upd.get("message") or upd.get("my_chat_member") or {}
                chat = msg.get("chat") or {}
                if chat.get("id"):
                    self.tg_chat = str(chat["id"])
                    self.state["telegram_chat_id"] = self.tg_chat
                    log.info("Telegram chat discovered")
                    return
            log.warning("Telegram token set but no chat yet: open your bot in Telegram and send /start")
        except (requests.RequestException, ValueError) as e:
            log.warning("Telegram getUpdates failed: %s", type(e).__name__)

    def _telegram(self, text_html: str) -> bool:
        for attempt in range(3):
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                    json={"chat_id": self.tg_chat, "text": text_html, "parse_mode": "HTML",
                          "disable_web_page_preview": True},
                    timeout=20,
                )
                if r.status_code == 429:
                    wait = (r.json().get("parameters") or {}).get("retry_after", 3)
                    time.sleep(min(int(wait), 30))
                    continue
                if r.status_code != 200:
                    log.warning("Telegram send failed: HTTP %s %s", r.status_code, r.text[:200])
                    return False
                log.info("Telegram message sent")
                return True
            except requests.RequestException as e:
                log.warning("Telegram send error: %s", type(e).__name__)
                time.sleep(2)
        return False

    # -- ntfy
    def _ntfy(self, title: str, body: str, click: str | None = None, priority: int = 3) -> bool:
        payload = {"topic": self.ntfy_topic, "title": title, "message": body, "priority": priority}
        if click:
            payload["click"] = click
        try:
            r = requests.post(self.ntfy_server + "/", json=payload,
                              headers={"User-Agent": USER_AGENT}, timeout=20)
            if r.status_code >= 300:
                log.warning("ntfy send failed: HTTP %s", r.status_code)
                return False
            log.info("ntfy message sent")
            return True
        except requests.RequestException as e:
            log.warning("ntfy send error: %s", type(e).__name__)
            return False

    # -- public API
    def send_job(self, m: dict) -> None:
        if self.dry_run:
            log.info("[dry-run] alert: %s @ %s (%s)", m["title"], m["company"], m["score"])
            return
        if self.tg_token and self.tg_chat:
            self._telegram(format_job_html(m))
        if self.ntfy_topic:
            self._ntfy(f"{m['title']} — {m['company']}", format_job_text(m), click=m["url"],
                       priority=4 if m["score"] >= 70 else 3)
        self.sent += 1
        time.sleep(0.4)

    def send_text(self, text: str, title: str = "Job Radar", click: str | None = None) -> None:
        if self.dry_run:
            log.info("[dry-run] message: %s", text.replace("\n", " | ")[:300])
            return
        if self.tg_token and self.tg_chat:
            self._telegram(html.escape(text) + (f'\n<a href="{html.escape(click, quote=True)}">Open report</a>' if click else ""))
        if self.ntfy_topic:
            self._ntfy(title, text, click=click)
