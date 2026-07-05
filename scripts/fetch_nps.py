#!/usr/bin/env python3
"""Fetch Estonian electricity spot prices (Nord Pool day-ahead, EE zone)
from the Elering public API and emit a small hourly JSON snapshot.

Why this script exists:
  The canonical public source is Elering (the Estonian TSO):
      https://dashboard.elering.ee/api/nps/price
  It's free and needs no API key, BUT it sends no CORS headers, so the
  PWA can't fetch it directly from the browser. Same bridge pattern as
  EMHI: a GitHub Actions cron fetches it server-side, writes nps.json,
  and force-pushes it to the 'data' orphan branch, which the PWA reads
  via raw.githubusercontent.com (CORS-open).

Output shape (prices are HOURLY, UTC hour-start unix seconds, €/MWh
EXCLUDING VAT — the app converts to snt/kWh and adds VAT client-side so
the VAT rate stays adjustable without re-fetching):

    {
      "source": "Elering / Nord Pool day-ahead (EE)",
      "fetched_at": "2026-06-22T10:00:00+00:00",
      "vat_pct": 24,
      "prices": [ {"ts": 1750000000, "eur_mwh": 3.41}, ... ]
    }

Nord Pool moved to 15-minute market time units in 2025, so the API now
returns quarter-hourly points; we average them into hourly buckets.

Usage:
    python3 scripts/fetch_nps.py --out data/nps.json

Network hiccups are retried (3 attempts with backoff). If every attempt
fails the script writes no file, sets the Actions output ``wrote=false``,
and exits 0 — so a transient outage keeps the last good snapshot and
doesn't email a cron-failure alert.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

API = "https://dashboard.elering.ee/api/nps/price"

# Suggested default VAT for the Estonian spot component (24% since
# 2025-07-01). Carried in the JSON so the app can display it / toggle it.
VAT_PCT = 24

# How wide a window to fetch: enough to always cover the next 24 h from
# "now" plus tomorrow when it's published (~15:00 EET day-ahead).
HOURS_BACK = 25
HOURS_FWD = 50

FETCH_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (5, 15)


def fetch_raw() -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    start = (now - dt.timedelta(hours=HOURS_BACK)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end = (now + dt.timedelta(hours=HOURS_FWD)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    url = f"{API}?start={start}&end={end}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; MadiseIlmaradar/1.0; "
                "+https://github.com/indrekraag/wa3)"
            ),
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Elering returned HTTP {resp.status}")
        return json.loads(resp.read().decode("utf-8"))


def build_snapshot(raw: dict) -> dict:
    ee = ((raw or {}).get("data") or {}).get("ee") or []
    if not ee:
        raise RuntimeError("Elering returned no EE price points")

    # Average the (now 15-min) points into hourly buckets keyed by the
    # UTC hour-start unix second.
    buckets: dict[int, list] = defaultdict(list)
    for p in ee:
        ts = p.get("timestamp")
        price = p.get("price")
        if ts is None or price is None:
            continue
        hour_start = int(ts) - (int(ts) % 3600)
        buckets[hour_start].append(float(price))

    if not buckets:
        raise RuntimeError("Elering points had no usable timestamp/price")

    prices = [
        {"ts": h, "eur_mwh": round(sum(v) / len(v), 2)}
        for h, v in sorted(buckets.items())
    ]
    return {
        "source": "Elering / Nord Pool day-ahead (EE)",
        "source_url": API,
        "fetched_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "vat_pct": VAT_PCT,
        "currency_note": "eur_mwh excludes VAT; snt/kWh = eur_mwh/10, then * (1+vat/100)",
        "prices": prices,
    }


def set_action_output(name: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(f"{name}={value}\n")


def fetch_with_retries(attempts: int = FETCH_ATTEMPTS):
    last_err = None
    for i in range(1, attempts + 1):
        try:
            return build_snapshot(fetch_raw())
        except Exception as exc:  # noqa: BLE001 — catch-all is intentional for retry
            last_err = exc
            print(f"NPS fetch attempt {i}/{attempts} failed: {exc}", file=sys.stderr)
            if i < attempts:
                delay = RETRY_BACKOFF_SECONDS[min(i - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
                print(f"  retrying in {delay}s…", file=sys.stderr)
                time.sleep(delay)
    print(f"NPS fetch failed after {attempts} attempts; last error: {last_err}", file=sys.stderr)
    return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default="data/nps.json")
    args = p.parse_args()

    snap = fetch_with_retries()
    if snap is None:
        print(
            "Soft failure: Elering unreachable after retries — keeping the "
            "previous snapshot (no file written, no push)."
        )
        set_action_output("wrote", "false")
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_path} — {len(snap['prices'])} hourly points, vat={snap['vat_pct']}%")
    set_action_output("wrote", "true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
