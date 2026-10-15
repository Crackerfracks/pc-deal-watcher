"""Overnight PC parts deal watcher.

Reads deal feeds, matches titles against watchlist.json, and sends a phone
alert through ntfy for each new deal at or under its target price.
Standard library only, so GitHub Actions needs no installs.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).parent
SEEN_PATH = ROOT / "state" / "seen.json"
SEEN_MAX_AGE_DAYS = 30
# Slickdeals searches also return deals from months ago; skip anything older.
DEAL_MAX_AGE_DAYS = 3
USER_AGENT = "pc-deal-watcher/1.0 (personal price alerts; github.com/Crackerfracks)"
PRICE_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
# Single parts shouldn't match whole-computer deals unless the entry says so.
WHOLE_PC_WORDS = ["prebuilt", "laptop", "gaming desktop", "gaming pc"]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def reddit_deals(sub):
    data = json.loads(fetch(f"https://www.reddit.com/r/{sub}/new.json?limit=100"))
    for child in data["data"]["children"]:
        post = child["data"]
        yield {
            "id": "reddit:" + post["id"],
            "title": post["title"],
            "url": post.get("url_overridden_by_dest") or post.get("url"),
            "thread": "https://www.reddit.com" + post["permalink"],
            "expired": post.get("link_flair_text", "") in ("Expired", "Out of Stock")
            or post.get("over_18", False),
            "posted": post.get("created_utc"),
        }


def reddit_rss_deals(sub):
    # Fallback when the JSON endpoint refuses the runner's address.
    root = ET.fromstring(fetch(f"https://www.reddit.com/r/{sub}/new/.rss?limit=100"))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("a:entry", ns):
        link = entry.find("a:link", ns).get("href")
        yield {
            "id": "reddit:" + entry.find("a:id", ns).text.split("_")[-1],
            "title": entry.find("a:title", ns).text,
            "url": link,
            "thread": link,
            "expired": False,
            "posted": None,
        }


def slickdeals_deals(query):
    q = urllib.parse.quote_plus(query)
    url = f"https://slickdeals.net/newsearch.php?q={q}&searcharea=deals&searchin=first&rss=1"
    root = ET.fromstring(fetch(url))
    for item in root.iter("item"):
        link = item.findtext("link") or ""
        try:
            posted = parsedate_to_datetime(item.findtext("pubDate")).timestamp()
        except (TypeError, ValueError):
            posted = None
        yield {
            "id": "slickdeals:" + (item.findtext("guid") or link),
            "title": item.findtext("title") or "",
            "url": link,
            "thread": link,
            "expired": False,
            "posted": posted,
        }


def squash(text):
    return re.sub(r"\s+", "", text.lower())


def has(title, word):
    return squash(word) in title


def matches(part, title):
    t = squash(title)
    exclude = part.get("exclude", [])
    if not part.get("whole_pc"):
        exclude = exclude + WHOLE_PC_WORDS
    if any(has(t, w) for w in exclude):
        return False
    if not all(has(t, w) for w in part.get("all", [])):
        return False
    for key in ("any", "also_any"):
        words = part.get(key)
        if words and not any(has(t, w) for w in words):
            return False
    return True


def deal_price(part, title):
    # Titles often list several prices (list price, rebate, final). Ignore ones
    # too small to be the part price and take the last remaining one, which is
    # usually the final price.
    prices = [float(p.replace(",", "")) for p in PRICE_RE.findall(title)]
    prices = [p for p in prices if p >= part.get("min_price", 0)]
    return prices[-1] if prices else None


def notify(topic, title, message, click=None, priority="default"):
    headers = {"Title": title, "Priority": priority, "Tags": "computer"}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=message.encode(), headers=headers, method="POST"
    )
    urllib.request.urlopen(req, timeout=30).read()


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def gather(sources):
    deals, failures = [], []
    for sub in sources.get("reddit", []):
        try:
            deals += list(reddit_deals(sub))
        except Exception as e:
            try:
                deals += list(reddit_rss_deals(sub))
            except Exception as e2:
                failures.append(f"r/{sub}: {e} / rss: {e2}")
        time.sleep(2)
    for query in sources.get("slickdeals", []):
        try:
            deals += list(slickdeals_deals(query))
        except Exception as e:
            failures.append(f"slickdeals '{query}': {e}")
        time.sleep(2)
    return deals, failures


def main():
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        sys.exit("NTFY_TOPIC is not set")
    if os.environ.get("SEND_TEST") == "true":
        notify(topic, "PC deal watcher test", "Alerts work. Real deals will show up here overnight.")

    parts = load_json(ROOT / "watchlist.json", {})["parts"]
    sources = load_json(ROOT / "sources.json", {})
    seen = load_json(SEEN_PATH, {})
    now = time.time()

    deals, failures = gather(sources)
    print(f"Fetched {len(deals)} deal titles, {len(failures)} feed failures")
    for f in failures:
        print("  FAILED", f)

    alerts = 0
    for deal in deals:
        if deal["id"] in seen or deal["expired"]:
            continue
        seen[deal["id"]] = now
        if deal["posted"] and now - deal["posted"] > DEAL_MAX_AGE_DAYS * 86400:
            continue
        for part in parts:
            if not matches(part, deal["title"]):
                continue
            price = deal_price(part, deal["title"])
            if price is None or price > part["max_price"]:
                continue
            print(f"MATCH {part['name']} ${price}: {deal['title']}")
            notify(
                topic,
                f"{part['name']}: ${price:g} (target ${part['max_price']})",
                f"{deal['title']}\n\nDiscussion: {deal['thread']}",
                click=deal["url"],
                priority="high",
            )
            alerts += 1
            break

    # Every feed failing means alerts have silently stopped; say so.
    total = len(sources.get("reddit", [])) + len(sources.get("slickdeals", []))
    if total and len(failures) == total:
        notify(topic, "PC deal watcher: all feeds failed", "\n".join(failures)[:3000], priority="high")

    cutoff = now - SEEN_MAX_AGE_DAYS * 86400
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_PATH.parent.mkdir(exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, indent=0, sort_keys=True))
    print(f"Sent {alerts} alerts")


if __name__ == "__main__":
    main()
