#!/usr/bin/env python3
"""PRG -> TPA December flight price monitor.

Reads the round-trip price grid that Google Flights embeds in its initial
page (the same numbers shown in its "price graph"), filters to December
outbound / Christmas-New-Year return dates, then verifies the itinerary
(stops, total duration, layover length) for the cheapest candidate date
pairs by parsing Google Flights' flight cards. Pushes Telegram alerts for
qualifying new all-time lows and target-price hits.

Runs twice a day from GitHub Actions (08:00 and 18:00 CET).
"""

import json
import os
import re
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
TARGET_PRICE = float(os.getenv("TARGET_PRICE_EUR", "800"))

# Inclusive date bounds for the grid entries we care about.
MIN_OUT, MAX_OUT = "2026-12-07", "2026-12-15"
MIN_RET, MAX_RET = "2026-12-24", "2027-01-03"

# Two nearby searches give a wide grid of date-pair prices.
BASE_QUERIES = [
    ("2026-12-10", "2026-12-27"),
    ("2026-12-12", "2027-01-02"),
]

# Itinerary constraints (applied to EACH direction independently).
MAX_STOPS = int(os.getenv("MAX_STOPS", "1"))
MAX_TOTAL_MINUTES = int(os.getenv("MAX_TOTAL_HOURS", "16")) * 60
MAX_LAYOVER_MINUTES = int(os.getenv("MAX_LAYOVER_MINUTES", "240"))
MAX_CANDIDATES_TO_CHECK = int(os.getenv("MAX_CANDIDATES_TO_CHECK", "12"))

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]
HISTORY = Path(os.getenv("HISTORY_FILE", "history.json"))

# Cache for itinerary page HTML keyed by (src, dst, date).
_ITIN_CACHE = {}


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
def _client():
    return Client(
        impersonate="chrome_145",
        impersonate_os="macos",
        referer=True,
        cookie_store=True,
    )


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
    client = _client()

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

    script_text = script.text()
    marker = "data:"
    start = script_text.find(marker)
    if start < 0:
        raise RuntimeError("price-grid data not found in script")
    # Google sometimes appends extra JavaScript after the JSON literal, so use
    # raw_decode to read just the first complete JSON value.
    payload, _ = json.JSONDecoder().raw_decode(
        script_text[start + len(marker) :].lstrip()
    )
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


def fetch_itinerary_html(src: str, dst: str, date: str) -> str:
    """Fetch Google Flights page for a single leg; cached."""
    key = (src, dst, date)
    if key in _ITIN_CACHE:
        return _ITIN_CACHE[key]

    client = _client()
    last_error = None
    for attempt in range(3):
        try:
            html = client.get(
                "https://www.google.com/travel/flights",
                params={"q": f"Flights from {src} to {dst} on {date}", "ucbcb": "1"},
                timeout=45,
            ).text
            if "consent.google.com" in html[:2000]:
                raise RuntimeError("Google consent page returned")
            _ITIN_CACHE[key] = html
            return html
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"itinerary fetch failed after 3 attempts: {last_error}")


# ---------------------------------------------------------- itinerary parse
def _parse_stops(text: str):
    m = re.search(r"(\d+)\s*stop", text)
    if m:
        return int(m.group(1))
    if "nonstop" in text.lower() or "non-stop" in text.lower():
        return 0
    return None


def _parse_duration_minutes(text: str):
    m = re.search(r"Total duration (\d+)\s*hr(?:\s*(\d+)\s*min)?", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2) or 0)
    m = re.search(r"Total duration (\d+)\s*min", text)
    if m:
        return int(m.group(1))
    return None


def _parse_layover_minutes(text: str):
    out = []
    for m in re.finditer(r"Layover \(\d+ of \d+\) is a (.+?) layover", text):
        seg = m.group(1)
        mins = 0
        h = re.search(r"(\d+)\s*hr", seg)
        mn = re.search(r"(\d+)\s*min", seg)
        if h:
            mins += int(h.group(1)) * 60
        if mn:
            mins += int(mn.group(1))
        out.append(mins)
    return out


def direction_qualifies(src: str, dst: str, date: str):
    """Return True/False if verifiable, None if scrape unavailable."""
    try:
        html = fetch_itinerary_html(src, dst, date)
    except Exception:
        return None
    labels = re.findall(r'aria-label="(From [^"]*)"', html)
    if not labels:
        return None
    found_any = False
    for label in labels:
        stops = _parse_stops(label)
        dur = _parse_duration_minutes(label)
        lays = _parse_layover_minutes(label)
        if stops is None or dur is None:
            continue
        found_any = True
        if (
            stops <= MAX_STOPS
            and dur <= MAX_TOTAL_MINUTES
            and all(l <= MAX_LAYOVER_MINUTES for l in lays)
        ):
            return True
    # If we parsed at least one itinerary but none qualified, it's a real "no".
    return False if found_any else None


def choose_candidate(results):
    """Pick cheapest qualifying candidate; fall back to first unknown/cheapest.

    Returns (candidate_dict, status_str, num_checked).
    status_str is one of: 'qualified', 'unknown', 'not_qualified'.
    """
    first_unknown = None
    checked = 0
    for r in results:
        if checked >= MAX_CANDIDATES_TO_CHECK:
            break
        checked += 1
        out_q = direction_qualifies(FROM, TO, r["out"])
        ret_q = direction_qualifies(TO, FROM, r["ret"])
        if out_q is False or ret_q is False:
            status = "not_qualified"
        elif out_q is True and ret_q is True:
            status = "qualified"
        else:
            status = "unknown"
        r["status"] = status
        if status == "qualified":
            return r, "qualified", checked
        if status == "unknown" and first_unknown is None:
            first_unknown = r

    if first_unknown is not None:
        return first_unknown, "unknown", checked
    if results:
        r = results[0]
        r.setdefault("status", "not_qualified")
        return r, "not_qualified", checked
    return None, "not_qualified", checked


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


def fmt(r, status):
    link = "https://www.google.com/travel/flights?q=" + quote(
        f"Flights from {FROM} to {TO} on {r['out']} returning {r['ret']}"
    )
    if status == "qualified":
        note = "✅ meets rules: ≤%d stop, ≤%dh, ≤%dmin layover" % (
            MAX_STOPS, MAX_TOTAL_MINUTES // 60, MAX_LAYOVER_MINUTES,
        )
    elif status == "unknown":
        note = "⚠️ itinerary not verified (scrape unavailable)"
    else:
        note = "❌ no itinerary met the rules"
    return (
        f"✈️ {FROM}→{TO}  {r['out']} → {r['ret']}\n"
        f"💶 €{r['price']} round-trip (Google Flights price grid)\n"
        f"🔗 {link}\n"
        f"ℹ️ {note}"
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

    chosen, status, checked = choose_candidate(results)

    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    previous_low = history.get("all_time_low")
    # Ignore stale all-time lows recorded before itinerary filtering existed.
    if previous_low and previous_low.get("status") != "qualified":
        previous_low = None

    lines = [fmt(chosen, status)]
    if previous_low is None or chosen["price"] < previous_low.get("price", float("inf")):
        lines.append("🎉 NEW ALL-TIME LOW")
        history["all_time_low"] = {
            "price": chosen["price"],
            "ts": timestamp,
            "out": chosen["out"],
            "ret": chosen["ret"],
            "status": status,
        }
    if chosen["price"] <= TARGET_PRICE:
        if status == "qualified":
            lines.append(
                f"✅ At or below your €{TARGET_PRICE:.0f} target and meets itinerary rules — worth a look!"
            )
        else:
            lines.append(
                f"ℹ️ At or below your €{TARGET_PRICE:.0f} target, but itinerary did not fully meet the rules."
            )
    if status != "qualified":
        lines.append(
            f"⚠️ No qualifying itinerary (≤{MAX_STOPS} stop, "
            f"≤{MAX_TOTAL_MINUTES // 60}h, ≤{MAX_LAYOVER_MINUTES}min layover) "
            f"found among {checked} cheapest date pairs."
        )
    if failures:
        lines.append(
            f"⚠️ {len(failures)}/{len(BASE_QUERIES)} price-grid lookups failed: "
            + failures[0]
        )

    message = "\n".join(lines)
    telegram(message)
    print(message)

    history["last_run"] = timestamp
    history["last_price"] = chosen["price"]
    history["last_status"] = status
    history.setdefault("runs", []).append(
        {
            "ts": timestamp,
            "price": chosen["price"],
            "out": chosen["out"],
            "ret": chosen["ret"],
            "status": status,
        }
    )
    history["runs"] = history["runs"][-200:]
    HISTORY.write_text(json.dumps(history, indent=2) + "\n")


if __name__ == "__main__":
    main()
