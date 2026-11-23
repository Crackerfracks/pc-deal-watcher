# pc-deal-watcher

Checks PC part deal feeds every 2 hours and sends a phone alert through the ntfy app when a part on the list drops to its target price. A free cron-job.org timer starts it at 7 past every odd hour, Eastern, so no computer needs to be on, and it costs nothing.

- **See current deals:** [BOARD.md](BOARD.md), grouped by part, refreshed every run.
- **Phone alerts:** by default everything goes to the main ntfy channel (the `NTFY_TOPIC` secret), with the part type at the start of the title, e.g. `[GPU]`. Set `"alert_channels": "per-part"` in `watchlist.json` to send each part type to its own channel instead (`-gpu`, `-cpu`, `-motherboard`, `-ram`, `-storage`, `-power`, `-case-cooling`, `-prebuilt`).
- **Great deals:** a part 10%+ and $25+ under target, or a prebuilt up to $1,800 worth $250+ more than buying its parts (`judge.py` estimates that), sends an urgent alert that repeats every 6 hours for up to 2 days until it's acknowledged in the Claude PC parts thread (Claude adds the deal id to `state/acks.json`). `state/flags.json` lists what needs a human, for a daily Claude check-in.
- **What it watches:** `watchlist.json` (each part, the words to match, and the target price). Change a `max_price` to raise or lower a target.
- **Where it looks:** `sources.json` (r/buildapcsales and Slickdeals searches).
- **Run it now:** Actions tab, "Watch for PC part deals", "Run workflow". Tick "send a test alert" to ping every channel, or "backfill" to refill the board without alerts.
- **Alert channel:** stored as the `NTFY_TOPIC` repository secret.

The parts list and reasoning live in the project notes (`plans/pc-build/builds.md`).
