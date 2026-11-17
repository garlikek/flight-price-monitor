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
  the target price (default €900).
- Alerts include a direct Google Flights link for the winning dates.
- `history.json` stores the all-time low and recent runs for drop detection.

## Schedule

GitHub Actions runs at **08:00 and 18:00 CET** (`cron: '0 7,17 * * *'` UTC).
You can also trigger it manually from the Actions tab ("Run workflow").

## Configuration

- `TARGET_PRICE_EUR` — alert threshold (default `900`), set in the workflow env.
- `MIN_OUT` / `MAX_OUT` / `MIN_RET` / `MAX_RET` and `BASE_QUERIES` in `monitor.py`.

Secrets (repo Settings → Secrets and variables → Actions):

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Caveats

- The price grid reports fares only; it does not expose stops or layover
  lengths. Check the linked itinerary before booking if the 16h / no-long-layover
  rule matters for a given fare.
- If Google changes the page layout or blocks the runner, the bot sends a
  warning and the next scheduled run retries.
