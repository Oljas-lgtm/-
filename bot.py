"""Telegram crypto news bot.

Reads RSS feeds and posts fresh items (headline + short snippet + link) to a
Telegram channel. Meant to run on a schedule (e.g. Render cron job every 30-60 min).

Environment variables
  BOT_TOKEN          (required) token from @BotFather
  CHANNEL_ID         (required) @channel_username or numeric id like -100123...
  FEEDS              comma-separated RSS URLs (default: Cointelegraph, CoinDesk)
  MAX_POSTS_PER_RUN  max posts per run (default 3)
  WINDOW_MINUTES     without Redis: only items newer than this (default 90)
  REDIS_URL          optional, enables reliable de-duplication
  ANTHROPIC_API_KEY  optional, translates posts to Russian via Claude
  TRANSLATE_MODEL    optional, default claude-haiku-5-5
  POST_FOOTER        optional text appended to every post
"""
import calendar
import html
import os
import re
import time

import feedparser
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
FEEDS = [
    u.strip()
    for u in os.environ.get(
        "FEEDS",
        "https://cointelegraph.com/rss,https://www.coindesk.com/arc/outboundfeeds/rss/",
    ).split(",")
    if u.strip()
]
MAX_POSTS = int(os.environ.get("MAX_POSTS_PER_RUN", "3"))
WINDOW_MIN = int(os.environ.get("WINDOW_MINUTES", "90"))
REDIS_URL = os.environ.get("REDIS_URL")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
TRANSLATE_MODEL = os.environ.get("TRANSLATE_MODEL", "claude-haiku-5-5")
FOOTER = os.environ.get("POST_FOOTER", "")

# With Redis we remember what was posted, so a wide window is safe.
WINDOW_SEC = (24 * 60 if REDIS_URL else WINDOW_MIN) * 60


class Seen:
    """Remembers posted links in Redis (no-op if Redis is not configured)."""

    def __init__(self):
        self.r = None
        if REDIS_URL:
            import redis

            self.r = redis.from_url(REDIS_URL)

    def has(self, key):
        return bool(self.r.sismember("seen", key)) if self.r else False

    def add(self, key):
        if self.r:
            self.r.sadd("seen", key)
            self.r.expire("seen", 60 * 60 * 24 * 30)


def clean(text, limit):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def entry_ts(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return calendar.timegm(t) if t else None


def collect(seen):
    now = time.time()
    items = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url, agent="Mozilla/5.0 (crypto-news-bot)")
        except Exception as ex:
            print("Feed error:", url, type(ex).__name__)
            continue
        if not feed.entries:
            print("No entries (check the URL):", url)
        for e in feed.entries[:20]:
            link, title = e.get("link"), e.get("title")
            if not link or not title:
                continue
            ts = entry_ts(e) or now
            if now - ts > WINDOW_SEC or seen.has(link):
                continue
            items.append(
                {
                    "title": clean(title, 200),
                    "summary": clean(e.get("summary", ""), 280),
                    "link": link,
                    "ts": ts,
                }
            )
    items.sort(key=lambda i: i["ts"], reverse=True)
    return items


def translate(title, summary):
    """Translate to Russian with Claude if ANTHROPIC_API_KEY is set."""
    if not ANTHROPIC_KEY:
        return title, summary
    prompt = (
        "Переведи на русский язык заголовок и краткое описание новости. "
        "Не добавляй ничего от себя. Ответь ровно двумя строками: "
        "первая — заголовок, вторая — описание (если описания нет, оставь вторую строку пустой).\n\n"
        f"Заголовок: {title}\nОписание: {summary}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": TRANSLATE_MODEL,
                "max_tokens": 600,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json()["content"]).strip()
        lines = text.split("\n")
        new_title = lines[0].strip() or title
        new_summary = " ".join(l.strip() for l in lines[1:]).strip() if len(lines) > 1 else summary
        return new_title, new_summary
    except Exception as ex:
        print("Translation failed, posting original:", type(ex).__name__)
        return title, summary


def send(title, summary, link):
    text = f"<b>{html.escape(title)}</b>"
    if summary:
        text += f"\n\n{html.escape(summary)}"
    text += f'\n\n<a href="{html.escape(link, quote=True)}">Читать полностью</a>'
    if FOOTER:
        text += f"\n\n{html.escape(FOOTER)}"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHANNEL_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=30,
        )
    except Exception as ex:
        # Do not print the exception text: it can contain the bot token in the URL.
        print("Telegram network error:", type(ex).__name__)
        return False
    if not r.ok:
        print("Telegram error:", r.status_code, r.text[:300])
        return False
    return True


def main():
    seen = Seen()
    items = collect(seen)
    posted = 0
    for it in items:
        if posted >= MAX_POSTS:
            seen.add(it["link"])  # skip the backlog so it is not posted later
            continue
        title, summary = translate(it["title"], it["summary"])
        if send(title, summary, it["link"]):
            posted += 1
            seen.add(it["link"])
            time.sleep(3)
    print(f"Done. Candidates: {len(items)}, posted: {posted}")


if __name__ == "__main__":
    main()
