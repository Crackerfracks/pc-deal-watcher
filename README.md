# pc-deal-watcher

Checks PC part deal feeds three times every morning (between 4 and 9:30 AM Eastern) and sends a phone alert through the ntfy app when a part on the list drops to its target price. It runs on GitHub's free scheduler, so no computer needs to be on, and it costs nothing.

- **What it watches:** `watchlist.json` (each part, the words to match, and the target price). Change a `max_price` to raise or lower a target.
- **Where it looks:** `sources.json` (r/buildapcsales and Slickdeals searches).
- **Run it now:** Actions tab, "Watch for PC part deals", "Run workflow". Tick the box to also get a test alert.
- **Alert channel:** stored as the `NTFY_TOPIC` repository secret.

The parts list and reasoning live in the project notes (`plans/pc-build/target-build.md`).
