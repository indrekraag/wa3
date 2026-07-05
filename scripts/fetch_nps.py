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
EXCLUDING VAT — the app converts to snt/kWh and adds VAT client-side).
Elering serves the EE/FI/LV/LT Nord Pool zones from one call; we emit
them all under `zones` and mirror EE at the top level for compatibility:

    {
      "source": "Elering / Nord Pool day-ahead (EE, FI, LV, LT)",
      "fetched_at": "2026-06-22T10:00:00+00:00",
      "vat_pct": 24,
      "prices": [ {"ts": 1750000000, "eur_mwh": 3.41}, ... ],   # = EE zone
      "zones": {
        "ee": {"vat_pct": 24,   "prices": [...]},
        "fi": {"vat_pct": 25.5, "prices": [...]}, ...
      }
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

# Per-country VAT (standard rate) applied to the spot component, carried
# in the JSON so the app can display it without re-fetching. Elering
# serves the EE, FI, LV and LT Nord Pool zones from a single call.
# (EE 24% since 2025-07-01; FI 25.5% since 2024-09-01; LV/LT 21%.)
ZONE_VAT = {"ee": 24, "fi": 25.5, "lv": 21, "lt": 21}
ZONE_ORDER = ["ee", "fi", "lv", "lt"]

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


def _hourly(points: list) -> list:
    """Average Elering's (now 15-min) points into hourly buckets keyed by
    the UTC hour-start unix second."""
    buckets: dict[int, list] = defaultdict(list)
    for p in points or []:
        ts = p.get("timestamp")
        price = p.get("price")
        if ts is None or price is None:
            continue
        hour_start = int(ts) - (int(ts) % 3600)
        buckets[hour_start].append(float(price))
    return [
        {"ts": h, "eur_mwh": round(sum(v) / len(v), 2)}
        for h, v in sorted(buckets.items())
    ]


def build_snapshot(raw: dict) -> dict:
    data = (raw or {}).get("data") or {}
    if not (data.get("ee") or []):
        raise RuntimeError("Elering returned no EE price points")

    # Build every zone Elering returned (EE, FI, LV, LT). The PWA picks
    # the zone matching the phone's current country; EE is the default.
    zones: dict[str, dict] = {}
    for z in ZONE_ORDER:
        prices = _hourly(data.get(z) or [])
        if prices:
            zones[z] = {"vat_pct": ZONE_VAT.get(z, 24), "prices": prices}

    if "ee" not in zones:
        raise RuntimeError("Elering EE points had no usable timestamp/price")

    return {
        "source": "Elering / Nord Pool day-ahead (EE, FI, LV, LT)",
        "source_url": API,
        "fetched_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        # Top-level vat_pct/prices mirror the EE zone for backward compat.
        "vat_pct": ZONE_VAT["ee"],
        "prices": zones["ee"]["prices"],
        "zones": zones,
        "currency_note": "eur_mwh excludes VAT; snt/kWh = eur_mwh/10, then * (1+vat/100). Per-zone VAT in zones[*].vat_pct.",
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
    print(f"Wrote {out_path} — zones={list(snap['zones'])}, EE {len(snap['prices'])} hourly points")
    set_action_output("wrote", "true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
