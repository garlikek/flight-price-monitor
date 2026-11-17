#!/usr/bin/env python3
"""PRG -> TPA December flight price monitor.

Polls Google Flights (through the keyless ``fast-flights`` library) for a
curated set of December date pairs, drops itineraries that violate the trip
constraints, and pushes Telegram alerts for new all-time lows / target hits.

Runs twice a day from GitHub Actions (08:00 and 18:00 CET).
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from fast_flights import FlightQuery, create_query, get_flights

# ---------------------------------------------------------------- config
FROM = os.getenv("FROM_AIRPORT", "PRG")
TO = os.getenv("TO_AIRPORT", "TPA")
CURRENCY = os.getenv("CURRENCY", "EUR")
TARGET_PRICE = float(os.getenv("TARGET_PRICE_EUR", "900"))
MAX_LEG_MINUTES = int(os.getenv("MAX_LEG_MINUTES", "960"))  # 16h per direction
MAX_LAYOVER_MINUTES = int(os.getenv("MAX_LAYOVER_MINUTES", "180"))
MAX_STOPS = int(os.getenv("MAX_STOPS", "2"))

# ~2 weeks in December, returning between Christmas and New Year.
# Edit this list to change which date pairs are checked.
DATE_PAIRS = [
    ("2026-12-10", "2026-12-27"),
    ("2026-12-10", "2026-12-30"),
    ("2026-12-12", "2026-12-28"),
    ("2026-12-12", "2027-01-02"),
    ("2026-12-08", "2026-12-29"),
    ("2026-12-11", "2027-01-01"),
]

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]
HISTORY = Path(os.getenv("HISTORY_FILE", "history.json"))


# ------------------------------------------------------------- telegram
def telegram(text: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=25,
    )
    response.raise_for_status()


# ----------------------------------------------------------- itineraries
def _as_datetime(dt):
    return datetime(*dt.date, *dt.time)


def leg_stats(segments):
    """Duration, stop count and longest layover for one direction."""
    if not segments:
        return None
    duration = sum(s.duration for s in segments)
    layovers = []
    for arrival, departure in zip(segments, segments[1:]):
        gap = _as_datetime(departure.departure) - _as_datetime(arrival.arrival)
        layovers.append(int(gap.total_seconds() // 60))
    return {
        "duration": duration,
        "stops": len(segments) - 1,
        "max_layover": max(layovers) if layovers else 0,
    }


def split_legs(segments):
    """Split an itinerary into (outbound, return) using the origin airports."""
    legs = [[], []]
    for segment in segments:
        if segment.from_airport.code == TO and not legs[1]:
            legs[1].append(segment)
        elif legs[1]:
            legs[1].append(segment)
        else:
            legs[0].append(segment)
    return legs


def qualifies(f):
    legs = split_legs(f.flights)
    if not legs[0] or not legs[1]:
        return False  # we want a complete round trip
    for leg in legs:
        stats = leg_stats(leg)
        if stats["stops"] > MAX_STOPS:
            return False
        if stats["duration"] > MAX_LEG_MINUTES:
            return False
        if stats["max_layover"] > MAX_LAYOVER_MINUTES:
            return False
    return True


def summarise(f, out_date, ret_date):
    legs = split_legs(f.flights)
    out = leg_stats(legs[0])
    back = leg_stats(legs[1])
    return {
        "price": f.price,
        "out": out_date,
        "ret": ret_date,
        "airlines": sorted(set(f.airlines)),
        "out_stops": out["stops"],
        "ret_stops": back["stops"],
        "out_duration": out["duration"],
        "ret_duration": back["duration"],
        "max_layover": max(out["max_layover"], back["max_layover"]),
    }


# ------------------------------------------------------------------ scan
def scan():
    results, failures = [], []
    for out_date, ret_date in DATE_PAIRS:
        query = create_query(
            flights=[
                FlightQuery(
                    date=out_date,
                    from_airport=FROM,
                    to_airport=TO,
                    max_stops=MAX_STOPS,
                ),
                FlightQuery(
                    date=ret_date,
                    from_airport=TO,
                    to_airport=FROM,
                    max_stops=MAX_STOPS,
                ),
            ],
            trip="round-trip",
            currency=CURRENCY,
            language="en-US",
        )
        try:
            flights = get_flights(query)
            for f in flights:
                if qualifies(f):
                    results.append(summarise(f, out_date, ret_date))
        except Exception as exc:  # source blocked / changed layout / no flights
            failures.append(f"{out_date}->{ret_date}: {type(exc).__name__}: {exc}")
    return results, failures


def fmt(r):
    def hm(minutes):
        return f"{minutes // 60}h{minutes % 60:02d}m"

    return (
        f"✈️ {FROM}→{TO}  {r['out']} → {r['ret']}\n"
        f"💶 €{r['price']} round-trip\n"
        f"🛫 out: {r['out_stops']} stop(s), {hm(r['out_duration'])}\n"
        f"🛬 back: {r['ret_stops']} stop(s), {hm(r['ret_duration'])}\n"
        f"⏱️ longest layover {hm(r['max_layover'])}\n"
        f"🏢 {', '.join(r['airlines'][:4]) or 'multiple airlines'}"
    )


# ------------------------------------------------------------------ main
def main():
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results, failures = scan()

    if not results:
        msg = f"⚠️ {FROM}→{TO} monitor: no qualifying itineraries found this run."
        if failures:
            msg += "\nFetch errors:\n" + "\n".join(failures[:3])
        telegram(msg)
        print(msg)
        return

    best = min(results, key=lambda r: r["price"])
    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    previous_low = history.get("all_time_low")

    lines = [fmt(best)]
    if previous_low is None or best["price"] < previous_low["price"]:
        lines.append("🎉 NEW ALL-TIME LOW")
        history["all_time_low"] = {"price": best["price"], "ts": timestamp}
    if best["price"] <= TARGET_PRICE:
        lines.append(f"✅ At or below your €{TARGET_PRICE:.0f} target — book it!")
    if failures:
        lines.append(f"⚠️ {len(failures)}/{len(DATE_PAIRS)} date-pair lookups failed")

    message = "\n".join(lines)
    telegram(message)
    print(message)

    history["last_run"] = timestamp
    history["last_price"] = best["price"]
    history.setdefault("runs", []).append(
        {"ts": timestamp, "price": best["price"], "out": best["out"], "ret": best["ret"]}
    )
    history["runs"] = history["runs"][-200:]
    HISTORY.write_text(json.dumps(history, indent=2) + "\n")


if __name__ == "__main__":
    main()
