# PRG → TPA flight price monitor

Watches return-trip fares from Prague (PRG) to Tampa (TPA) for a ~2-week
December trip (outbound early December, return between Christmas and New Year)
and sends alerts to Telegram.

## How it works

- `monitor.py` queries Google Flights via the keyless `fast-flights` library
  for 6 curated date pairs.
- It filters out itineraries with more than 2 stops per direction, a leg over
  16 hours, or a layover over 3 hours.
- It alerts on a new all-time low, or whenever the cheapest qualifying fare is
  at or below the target price (default €900).
- `history.json` stores the all-time low and recent runs so drops are detected.

## Schedule

GitHub Actions runs at **08:00 and 18:00 CET** (`cron: '0 7,17 * * *'` UTC).
You can also trigger it manually from the Actions tab ("Run workflow").

## Configuration

Edit these values in `.github/workflows/flight-monitor.yml` env block or in
`monitor.py`:

- `TARGET_PRICE_EUR` (default `900`)
- `MAX_LEG_MINUTES` (default `960` = 16h per direction)
- `MAX_LAYOVER_MINUTES` (default `180`)
- `MAX_STOPS` (default `2`)
- `DATE_PAIRS` in `monitor.py`

Secrets (repo Settings → Secrets and variables → Actions):

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Note

Google Flights occasionally serves an EU consent page to datacenter IPs. When
that happens the affected lookups fail, the bot sends a "no qualifying
itineraries" warning, and the next scheduled run retries. No API key is needed.
