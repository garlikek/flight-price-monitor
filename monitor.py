#!/usr/bin/env python3
"""PRG -> TPA December flight price monitor.

Reads the round-trip price grid that Google Flights embeds in its initial
page (the same numbers shown in its "price graph"), filters to December
outbound / Christmas-New-Year return dates, and pushes Telegram alerts for
new all-time lows and target-price hits.

Runs twice a day from GitHub Actions (08:00 and 18:00 CET).
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from fast_flights import FlightQuery, create_query
from primp import Client
from selectolax.lexbor import LexborHTMLParser

# ---------------------------------------------------------------- config
FROM = os.getenv("FROM_AIRPORT", "PRG")
TO = os.getenv("TO_AIRPORT", "TPA")
CURRENCY = os.getenv("CURRENCY", "EUR")
TARGET_PRICE = float(os.getenv("TARGET_PRICE_EUR", "900"))

# Inclusive date bounds for the grid entries we care about.
MIN_OUT, MAX_OUT = "2026-12-07", "2026-12-15"
MIN_RET, MAX_RET = "2026-12-24", "2027-01-03"

# Two nearby searches give a wide grid of date-pair prices.
BASE_QUERIES = [
    ("2026-12-10", "2026-12-27"),
    ("2026-12-12", "2027-01-02"),
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


# --------------------------------------------------------------- scraping
def fetch_grid(out_date: str, ret_date: str):
    """Return [(out_date, ret_date, price), ...] from Google's price grid."""
    query = create_query(
        flights=[
            FlightQuery(date=out_date, from_airport=FROM, to_airport=TO),
            FlightQuery(date=ret_date, from_airport=TO, to_airport=FROM),
        ],
        trip="round-trip",
        currency=CURRENCY,
        language="en-US",
    )
    # ucbcb=1 skips the EU consent interstitial returned to datacenter IPs.
    params = {**query.params(), "ucbcb": "1"}
    client = Client(
        impersonate="chrome_145",
        impersonate_os="macos",
        referer=True,
        cookie_store=True,
    )

    last_error = None
    for attempt in range(3):
        try:
            html = client.get(
                "https://www.google.com/travel/flights", params=params, timeout=45
            ).text
            if "consent.google.com" in html[:2000]:
                raise RuntimeError("Google consent page returned")
            break
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError(f"Google fetch failed after 3 attempts: {last_error}")

    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        raise RuntimeError("price-grid script not found (blocked or layout changed)")

    payload = json.loads(script.text().split("data:", 1)[1].rsplit(",", 1)[0])
    grid = payload[6][0][4][0]  # list of [out_date, ret_date, [price, token], ...]

    entries = []
    for item in grid:
        try:
            price = item[2][0][1]
            if not isinstance(price, int) or price <= 0:
                continue
            entries.append((item[0], item[1], price))
        except (IndexError, TypeError):
            continue
    if not entries:
        raise RuntimeError("no date/price entries in grid")
    return entries


def scan():
    best_by_dates = {}
    failures = []
    for out_date, ret_date in BASE_QUERIES:
        try:
            entries = fetch_grid(out_date, ret_date)
            for out, back, price in entries:
                if not (MIN_OUT <= out <= MAX_OUT and MIN_RET <= back <= MAX_RET):
                    continue
                key = (out, back)
                if key not in best_by_dates or price < best_by_dates[key][2]:
                    best_by_dates[key] = (out, back, price)
        except Exception as exc:
            failures.append(f"{out_date}->{ret_date}: {type(exc).__name__}: {exc}")

    results = [
        {"out": out, "ret": back, "price": price}
        for out, back, price in best_by_dates.values()
    ]
    results.sort(key=lambda r: r["price"])
    return results, failures


def fmt(r):
    link = "https://www.google.com/travel/flights?q=" + quote(
        f"Flights from {FROM} to {TO} on {r['out']} returning {r['ret']}"
    )
    return (
        f"✈️ {FROM}→{TO}  {r['out']} → {r['ret']}\n"
        f"💶 €{r['price']} round-trip (Google Flights price grid)\n"
        f"🔗 {link}\n"
        f"ℹ️ Price only — check stops/layovers before booking"
    )


# ------------------------------------------------------------------ main
def main():
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results, failures = scan()

    if not results:
        msg = f"⚠️ {FROM}→{TO} monitor: no prices found this run."
        if failures:
            msg += "\nFetch errors:\n" + "\n".join(failures)
        telegram(msg)
        print(msg)
        return

    cheapest = results[0]
    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    previous_low = history.get("all_time_low")

    lines = [fmt(cheapest)]
    if previous_low is None or cheapest["price"] < previous_low["price"]:
        lines.append("🎉 NEW ALL-TIME LOW")
        history["all_time_low"] = {"price": cheapest["price"], "ts": timestamp}
    if cheapest["price"] <= TARGET_PRICE:
        lines.append(f"✅ At or below your €{TARGET_PRICE:.0f} target — worth a look!")
    if failures:
        lines.append(
            f"⚠️ {len(failures)}/{len(BASE_QUERIES)} lookups failed: "
            + failures[0]
        )

    message = "\n".join(lines)
    telegram(message)
    print(message)

    history["last_run"] = timestamp
    history["last_price"] = cheapest["price"]
    history.setdefault("runs", []).append(
        {
            "ts": timestamp,
            "price": cheapest["price"],
            "out": cheapest["out"],
            "ret": cheapest["ret"],
        }
    )
    history["runs"] = history["runs"][-200:]
    HISTORY.write_text(json.dumps(history, indent=2) + "\n")


if __name__ == "__main__":
    main()
