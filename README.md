# PRG → TPA flight price monitor

Watches round-trip fares from Prague (PRG) to Tampa (TPA) for a ~2-week
December trip (outbound early December, return between Christmas and New Year)
and sends alerts to Telegram twice a day.

## How it works

- `monitor.py` requests Google Flights for two nearby date pairs and reads the
  **price grid** Google embeds in the initial page (the numbers behind its
  "price graph"). This avoids the consent wall and the broken itinerary parser
  in `fast-flights`.
- It keeps date pairs with outbound 7–15 Dec and return 24 Dec – 3 Jan, then
  alerts on a new all-time low or whenever the cheapest fare is at or below
  the target price (default €800).
- Alerts include a direct Google Flights link for the winning dates.
- `history.json` stores the all-time low and recent runs for drop detection.

## Schedule

GitHub Actions runs at **08:00 and 18:00 CET** (`cron: '0 7,17 * * *'` UTC).
You can also trigger it manually from the Actions tab ("Run workflow").

## Configuration

- `TARGET_PRICE_EUR` — alert threshold (default `800`), set in the workflow env.
- `MIN_OUT` / `MAX_OUT` / `MIN_RET` / `MAX_RET` and `BASE_QUERIES` in `monitor.py`.

Secrets (repo Settings → Secrets and variables → Actions):

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Itinerary filtering

In addition to the price grid, the monitor parses Google Flights' flight cards
for the cheapest candidate date pairs and only treats a fare as "qualifying"
when **both** directions have:

- at most `MAX_STOPS` stops (default 1),
- total duration at most `MAX_TOTAL_HOURS` hours (default 16),
- every layover at most `MAX_LAYOVER_MINUTES` minutes (default 240).

Tunables are env vars: `MAX_STOPS`, `MAX_TOTAL_HOURS`, `MAX_LAYOVER_MINUTES`,
`MAX_CANDIDATES_TO_CHECK` (default 12 cheapest date pairs are checked).

## Caveats

- Itinerary details come from scraping Google Flights' flight cards; if Google
  changes the layout or blocks the runner, the monitor falls back to
  price-only alerts (marked "not verified") instead of dropping the alert.
- If Google blocks the runner entirely, the bot sends a warning and the next
  scheduled run retries.
