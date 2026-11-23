"""Overnight PC parts deal watcher.

Reads deal feeds, matches titles against watchlist.json, sends a phone alert
through ntfy for each new deal at or under its target price (one ntfy channel
per part type), and rewrites BOARD.md with every recent matching deal grouped
by part. Standard library only, so GitHub Actions needs no installs.
"""

import json
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import judge

ROOT = Path(__file__).parent
SEEN_PATH = ROOT / "state" / "seen.json"
DEALS_PATH = ROOT / "state" / "deals.json"
BOARD_PATH = ROOT / "BOARD.md"
ACKS_PATH = ROOT / "state" / "acks.json"
FLAGS_PATH = ROOT / "state" / "flags.json"
# Great deals re-alert every run until acknowledged, for at most this long.
URGENT_REPEAT_DAYS = 2
# Checks run every 2 hours, so space reminders out rather than sending one
# every run.
REMIND_EVERY_HOURS = 6
SEEN_MAX_AGE_DAYS = 30
# Slickdeals searches also return deals from months ago; skip anything older.
DEAL_MAX_AGE_DAYS = 3
BOARD_DAYS = 7
HISTORY_DAYS = 90
USER_AGENT = "pc-deal-watcher/1.0 (personal price alerts; github.com/Crackerfracks)"
SIZE_RE = re.compile(r"\b\d+\s?tb\b", re.I)
PRICE_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
# Single parts shouldn't match whole-computer deals unless the entry says so.
WHOLE_PC_WORDS = ["prebuilt", "laptop", "gaming desktop", "gaming pc"]
EXPIRED_FLAIRS = ("Expired", "Out of Stock", "Sold Out")
# In-store-only deals go on the board but don't alert unless the watchlist's
# "alert_in_store_only" is true.
# In-store-only deals: "in-store only", "Micro Center in-store", or a store
# named by city and state, like "In-Store Austin, TX".
IN_STORE_RE = re.compile(
    r"in[\s-]?store(?:\s+pickup)?\s+only"
    r"|micro\s?center\s+in[\s-]?store"
    r"|in[\s-]?store\s*[-:,(]?\s*[A-Za-z][A-Za-z .]+,\s*[A-Z]{2}\b",
    re.I,
)
CHANNELS = {
    "gpu": "Graphics cards",
    "cpu": "Processors",
    "motherboard": "Motherboards",
    "ram": "Memory (RAM)",
    "storage": "Storage (SSD)",
    "power": "Power supplies",
    "case-cooling": "Cases and cooling",
    "prebuilt": "Prebuilt PCs",
}
SHORT = {
    "gpu": "GPU", "cpu": "CPU", "motherboard": "BOARD", "ram": "RAM",
    "storage": "SSD", "power": "PSU", "case-cooling": "CASE/COOLER", "prebuilt": "PREBUILT",
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def reddit_post(post):
    return {
        "id": "reddit:" + post["id"],
        "title": post["title"],
        "url": post.get("url_overridden_by_dest") or post.get("url"),
        "thread": "https://www.reddit.com" + post["permalink"],
        "expired": (post.get("link_flair_text") or "") in EXPIRED_FLAIRS
        or post.get("over_18", False),
        "posted": post.get("created_utc"),
    }


def reddit_deals(sub):
    data = json.loads(fetch(f"https://www.reddit.com/r/{sub}/new.json?limit=100"))
    for child in data["data"]["children"]:
        yield reddit_post(child["data"])


def reddit_rss_deals(sub):
    # Fallback when the JSON endpoint refuses the runner's address.
    root = ET.fromstring(fetch(f"https://www.reddit.com/r/{sub}/new/.rss?limit=100"))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.findall("a:entry", ns):
        link = entry.find("a:link", ns).get("href")
        stamp = entry.findtext("a:published", namespaces=ns) or entry.findtext("a:updated", namespaces=ns)
        try:
            posted = datetime.fromisoformat(stamp).timestamp()
        except (TypeError, ValueError):
            posted = None
        yield {
            "id": "reddit:" + entry.find("a:id", ns).text.split("_")[-1],
            "title": entry.find("a:title", ns).text,
            "url": link,
            "thread": link,
            "expired": False,
            "posted": posted,
        }


def reddit_expired_ids(subs):
    """Ids of the newest posts the subreddits have flaired expired."""
    expired = set()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for sub in subs:
        url = f"https://www.reddit.com/r/{sub}/search.rss?q=flair%3AExpired&restrict_sr=1&sort=new&limit=100"
        root = ET.fromstring(fetch(url))
        for entry in root.findall("a:entry", ns):
            expired.add("reddit:" + entry.find("a:id", ns).text.split("_")[-1])
    return expired


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
    # Prices in parentheses are usually the breakdown, as in
    # "$2199.99 ($2999.99-$800)", so prefer prices outside them.
    def found(text):
        prices = [float(p.replace(",", "")) for p in PRICE_RE.findall(text)]
        return [p for p in prices if p >= part.get("min_price", 0)]
    # Titles that list several sizes ("2TB $199.99, 1TB $129.99") give each
    # size its own price: use the prices between this part's size and the next.
    sizes = list(SIZE_RE.finditer(title))
    if len({m.group(0).lower().replace(" ", "") for m in sizes}) > 1:
        want = {w.lower() for w in part.get("all", [])}
        for i, m in enumerate(sizes):
            if m.group(0).lower().replace(" ", "") in want:
                end = sizes[i + 1].start() if i + 1 < len(sizes) else len(title)
                prices = found(title[m.end():end])
                if prices:
                    return prices[0]
    prices = found(re.sub(r"\([^)]*\)", " ", title)) or found(title)
    return prices[-1] if prices else None


def notify(topic, title, message, click=None, priority="default", tags="computer"):
    headers = {"Title": title, "Priority": priority, "Tags": tags}
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


def hours(seconds):
    h = seconds / 3600
    return f"{h:.0f}h" if h < 48 else f"{h / 24:.0f} days"


def lifetimes(history, channel):
    """Hours that past deals in this channel stayed live before expiring."""
    return [
        d["expired_at"] - d["posted"]
        for d in history
        if d["channel"] == channel and d.get("expired_at") and d.get("posted")
    ]


def lifetime_note(history, channel):
    lives = lifetimes(history, channel)
    if len(lives) < 3:
        return "Not enough history yet to say how long deals like this last."
    return (
        f"Past {CHANNELS[channel].lower()} deals stayed live about "
        f"{hours(statistics.median(lives))} (median of {len(lives)})."
    )


def ago(ts, now):
    return hours(now - ts) + " ago" if ts else "unknown"


def write_board(history, parts, now):
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Deals board",
        "",
        f"Updated {stamp}. Every deal from the last {BOARD_DAYS} days that matched a part on the",
        "watch list, cheapest first. ✅ means at or under the target price (those also",
        "sent a phone alert). 🚨 means a great deal (10%+ and $25+ under target, or a prebuilt",
        "worth $250+ more than its parts). 🏬 means in-store pickup only (no alert sent).",
        "Prices come from deal titles, so open the link to confirm.",
        "",
    ]
    recent = [d for d in history if now - (d.get("posted") or d["first_seen"]) <= BOARD_DAYS * 86400]
    for channel, label in CHANNELS.items():
        lines += [f"## {label}", ""]
        channel_parts = [p for p in parts if p.get("channel") == channel]
        if not channel_parts:
            lines += ["Nothing watched here yet.", ""]
            continue
        for part in channel_parts:
            rows = sorted((d for d in recent if d["part"] == part["name"]), key=lambda d: d["price"])
            low = [d["price"] for d in history if d["part"] == part["name"]]
            low_note = f", lowest seen in {HISTORY_DAYS} days ${min(low):g}" if low else ""
            builds = f" — in {', '.join(part['builds'])}" if part.get("builds") else ""
            lines.append(f"### {part['name']} (target ${part['max_price']}{low_note}){builds}")
            lines.append("")
            if not rows:
                lines += ["No recent deals.", ""]
                continue
            lines += ["| Price | Deal | Posted | Status |", "|---|---|---|---|"]
            for d in rows:
                hit = "🏬 " if d.get("in_store") else (
                    "🚨 " if d.get("great") else ("✅ " if d["price"] <= part["max_price"] else "")
                )
                status = (
                    f"expired after ~{hours(d['expired_at'] - d['posted'])}"
                    if d.get("expired_at") and d.get("posted")
                    else ("expired" if d.get("expired_at") else "live as far as known")
                )
                title = d["title"].replace("|", "/")
                if d.get("verdict"):
                    title += f" — parts ~${d['verdict']['parts_value']:,}, saves ~${d['verdict']['savings']:,}"
                lines.append(f"| {hit}${d['price']:g} | [{title}]({d['url']}) | {ago(d.get('posted'), now)} | {status} |")
            lines.append("")
        lives = lifetimes(history, channel)
        if len(lives) >= 3:
            lines += [f"_{lifetime_note(history, channel)}_", ""]
    BOARD_PATH.write_text("\n".join(lines))


def verdict_text(v):
    return (
        f"Same parts bought separately: ~${v['parts_value']:,} (estimate), "
        f"so this saves ~${v['savings']:,}. " + "; ".join(v["notes"]) + "."
    )


def send_urgent(topic, d, part, reminder=False):
    why = verdict_text(d["verdict"]) if d.get("verdict") else (
        f"{(1 - d['price'] / part['max_price']) * 100:.0f}% under your ${part['max_price']} target."
    )
    builds = f" Part of: {', '.join(part['builds'])}." if part.get("builds") else ""
    notify(
        topic,
        f"{'REMINDER: ' if reminder else ''}GREAT DEAL [{SHORT[d['channel']]}] {d['part']}: ${d['price']:g}",
        f"{d['title']}\n\n{why}{builds}\n\nThis repeats each run until you tell Claude in the PC parts thread that you've seen it.\nDiscussion: {d['thread']}",
        click=d["url"],
        priority="urgent",
        tags="rotating_light",
    )


def main():
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        sys.exit("NTFY_TOPIC is not set")
    watchlist = load_json(ROOT / "watchlist.json", {})
    parts = watchlist["parts"]
    # "single" sends everything to the main channel with the part type in the
    # title; "per-part" sends to <topic>-<channel>.
    per_part = watchlist.get("alert_channels", "single") == "per-part"
    alert_in_store = watchlist.get("alert_in_store_only", False)

    def alert_topic(channel):
        return f"{topic}-{channel}" if per_part else topic

    if os.environ.get("SEND_CHANNEL_NAMES") == "true":
        # One message per channel, body is just the name, so it's easy to copy
        # into the ntfy app's subscribe box.
        for channel, label in CHANNELS.items():
            notify(topic, f"Subscribe: {label}", f"{topic}-{channel}", tags="bell")
            time.sleep(1)
    if os.environ.get("SEND_TEST") == "true" and not per_part:
        notify(topic, "[TEST] PC deal watcher", "Alerts work. Deals will arrive here with the part type at the start, like [GPU] or [RAM].")
    elif os.environ.get("SEND_TEST") == "true":
        for channel, label in CHANNELS.items():
            notify(alert_topic(channel), f"[{SHORT[channel]}] Test", f"Test alert for {label.lower()}. Alerts work.")
            time.sleep(1)

    sources = load_json(ROOT / "sources.json", {})
    seen = load_json(SEEN_PATH, {})
    history = load_json(DEALS_PATH, [])
    if not alert_in_store:
        for d in history:
            if IN_STORE_RE.search(d["title"]):
                d["in_store"], d["great"] = True, False
    now = time.time()

    deals, failures = gather(sources)
    print(f"Fetched {len(deals)} deal titles, {len(failures)} feed failures")
    for f in failures:
        print("  FAILED", f)

    # A backfill run fills the board from everything currently in the feeds
    # without sending alerts.
    backfill = os.environ.get("BACKFILL") == "true"
    known = {d["id"] for d in history}
    alerts = 0
    for deal in deals:
        if deal["expired"] or deal["id"] in known:
            continue
        if deal["id"] in seen and not backfill:
            continue
        seen[deal["id"]] = now
        max_age = BOARD_DAYS if backfill else DEAL_MAX_AGE_DAYS
        if deal["posted"] and now - deal["posted"] > max_age * 86400:
            continue
        for part in parts:
            if not matches(part, deal["title"]):
                continue
            price = deal_price(part, deal["title"])
            if price is None:
                continue
            verdict = judge.judge_prebuilt(deal["title"], price) if part.get("whole_pc") else None
            great = verdict["great"] if verdict else judge.is_great_part(part, price)
            in_store = bool(IN_STORE_RE.search(deal["title"])) and not alert_in_store
            if in_store:
                great = False
            # Keep near misses on the board so it shows what the market is doing.
            if price > part["max_price"] * 1.25 and not great:
                continue
            channel = part.get("channel", "gpu")
            entry = {
                "id": deal["id"], "part": part["name"], "channel": channel, "price": price,
                "title": deal["title"], "url": deal["url"], "thread": deal["thread"],
                "posted": deal["posted"], "first_seen": now, "great": great,
            }
            if verdict:
                entry["verdict"] = verdict
            if in_store:
                entry["in_store"] = True
            history.append(entry)
            known.add(deal["id"])
            if in_store or backfill:
                print(f"BOARD {part['name']} ${price}: {deal['title']}")
            elif great:
                print(f"GREAT {part['name']} ${price}: {deal['title']}")
                send_urgent(alert_topic(channel), entry, part)
                alerts += 1
            elif price <= part["max_price"]:
                print(f"ALERT {part['name']} ${price}: {deal['title']}")
                builds = f"\nPart of: {', '.join(part['builds'])}" if part.get("builds") else ""
                value = f"\n{verdict_text(verdict)}" if verdict else ""
                notify(
                    alert_topic(channel),
                    f"[{SHORT[channel]}] {part['name']}: ${price:g} (target ${part['max_price']})",
                    f"{deal['title']}{builds}{value}\n\n{lifetime_note(history, channel)}\nDiscussion: {deal['thread']}",
                    click=deal["url"],
                    priority="high",
                )
                alerts += 1
            else:
                print(f"BOARD {part['name']} ${price}: {deal['title']}")
            break

    # Check which reddit deals have since been flaired expired, to learn how
    # long deals last.
    if any(d["id"].startswith("reddit:") and not d.get("expired_at") for d in history):
        try:
            expired = reddit_expired_ids(sources.get("reddit", []))
            for d in history:
                if d["id"] in expired and not d.get("expired_at"):
                    d["expired_at"] = now
        except Exception as e:
            print("  FAILED expiry check:", e)

    # Every feed failing means alerts have silently stopped; say so.
    total = len(sources.get("reddit", [])) + len(sources.get("slickdeals", []))
    if total and len(failures) == total:
        notify(alert_topic("gpu"), "[WATCHER BROKEN] all deal feeds failed", "\n".join(failures)[:3000], priority="high")

    # Re-send great deals that haven't been acknowledged and haven't expired.
    acks = set(load_json(ACKS_PATH, []))
    by_name = {p["name"]: p for p in parts}
    pending = [
        d for d in history
        if d.get("great") and not d.get("in_store") and d["id"] not in acks and not d.get("expired_at")
        and now - d["first_seen"] <= URGENT_REPEAT_DAYS * 86400 and d["part"] in by_name
    ]
    for d in pending:
        last = d.get("last_alerted", d["first_seen"])
        if d["first_seen"] != now and not backfill and now - last >= REMIND_EVERY_HOURS * 3600:
            send_urgent(alert_topic(d["channel"]), d, by_name[d["part"]], reminder=True)
            d["last_alerted"] = now
    # For the daily check-in that tells Claude what needs a human.
    FLAGS_PATH.write_text(json.dumps({
        "checked_at": now,
        "great_unacknowledged": pending,
        "prebuilts_last_day": [
            d for d in history if d.get("verdict") and now - d["first_seen"] <= 86400
        ],
    }, indent=1))

    cutoff = now - SEEN_MAX_AGE_DAYS * 86400
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    history = [d for d in history if now - d["first_seen"] <= HISTORY_DAYS * 86400]
    SEEN_PATH.parent.mkdir(exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, indent=0, sort_keys=True))
    DEALS_PATH.write_text(json.dumps(history, indent=1))
    write_board(history, parts, now)
    print(f"Sent {alerts} alerts")


if __name__ == "__main__":
    main()
