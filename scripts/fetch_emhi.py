#!/usr/bin/env python3
"""Fetch EMHI (Estonian Weather Service) observations and emit a JSON
snapshot for one or more named stations.

Default target: Lääne-Nigula (WMO 26124), ~15 km north of Madise. The
station has full meteorological instrumentation: pressure, humidity,
visibility, phenomenon text, gust speed — fields the road weather
station at Kurevere doesn't provide.

Why this script exists:
  The EMHI XML endpoint at ilmateenistus.ee is fronted by Cloudflare with
  no CORS headers, so the PWA can't fetch it directly from the browser.
  This script runs in GitHub Actions on a 15-min cron, parses out the
  relevant station(s), and writes a small JSON file. That file is then
  force-pushed to a 'data' orphan branch in the repo, which the PWA reads
  via raw.githubusercontent.com (which DOES allow CORS).

Warnings are sourced from the MeteoAlarm Estonia + Finland ATOM feeds
(CAP 1.2) and tagged per warning with country (EE/FI), so wa3 can show
the set matching the phone's current location. Each warning's expiry
timestamp is checked at fetch time, so expired warnings never reach the
JSON. The English-language CAP <info> block is preferred so the `event`
field matches the app's translation table.

Usage:
    python3 scripts/fetch_emhi.py                       # default station(s)
    python3 scripts/fetch_emhi.py --stations 26124      # by WMO code
    python3 scripts/fetch_emhi.py --out data/emhi.json  # custom output

Network/Cloudflare hiccups are retried (3 attempts with backoff). If
every attempt fails, the script exits 0 *without* writing the output
file and sets the GitHub Actions step output ``wrote=false``, so the
workflow skips the push and the data branch keeps its last good
snapshot — a transient outage no longer emails a cron-failure alert.
Genuine bugs (argument errors, etc.) still raise and fail loudly.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

EMHI_URL = "https://www.ilmateenistus.ee/ilma_andmed/xml/observations.php"
# MeteoAlarm per-country CAP feeds. wa3 follows the phone across the
# EE↔FI border, so we fetch both and tag each warning with its country;
# the PWA shows only the set matching the phone's current country.
METEOALARM_FEEDS = [
    ("EE", "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-estonia"),
    ("FI", "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-finland"),
]

# wa3 follows the phone, so warnings are NOT filtered by county — every
# currently-active Estonian CAP warning is surfaced. (wa2 restricted this
# to Lääne county; that filter is intentionally dropped here.)

# How far ahead to surface upcoming warnings. Warnings with onset > now +
# this delta are dropped at fetch time so the dashboard doesn't get
# cluttered with multi-day-ahead alerts. Adjust as needed.
WARNING_LOOKAHEAD_HOURS = 24

# Fields we surface, in the order EMHI publishes them. Empty strings in
# the XML become None in the JSON so the PWA can detect "not measured".
FIELDS = [
    "phenomenon",
    "visibility",
    "precipitations",
    "airpressure",
    "relativehumidity",
    "airtemperature",
    "winddirection",
    "windspeed",
    "windspeedmax",
    "waterlevel",
    "waterlevel_eh2000",
    "watertemperature",
    "uvindex",
    "sunshineduration",
    "globalradiation",
]

DEFAULT_STATIONS = ["26124", "26123"]  # Lääne-Nigula, Haapsalu

# Retry policy for the EMHI fetch. EMHI sits behind Cloudflare and
# occasionally serves a slow response or an HTML challenge page; retrying
# a few times with a short backoff clears the vast majority of transient
# failures. Backoff applies *between* attempts, so 3 attempts sleep twice.
FETCH_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (5, 15)


def parse_float(s: str):
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_iso(s):
    """ISO 8601 → tz-aware UTC datetime. Returns None on failure.

    Handles the "Z" UTC suffix that fromisoformat() didn't accept reliably
    before Python 3.11, and normalises naive datetimes to UTC."""
    if not s:
        return None
    try:
        s = s.strip().replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def fetch_xml(url: str) -> bytes:
    # EMHI's Cloudflare rejects obviously-botty UAs; a realistic UA gets
    # through reliably. We also accept-encoding identity so the response
    # is plain XML (no gzip).
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; MadiseIlmaradar/1.0; "
                "+https://github.com/indrekraag/wa3)"
            ),
            "Accept": "text/xml, application/xml",
            "Accept-Language": "et, en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status != 200:
            raise RuntimeError(f"EMHI returned HTTP {resp.status}")
        return resp.read()


def extract_station(root: ET.Element, wmo: str) -> dict | None:
    for s in root.findall("station"):
        if (s.findtext("wmocode") or "").strip() == wmo:
            return s
    return None


def station_to_dict(s: ET.Element) -> dict:
    out = {
        "name": (s.findtext("name") or "").strip(),
        "wmocode": (s.findtext("wmocode") or "").strip(),
        "latitude": parse_float(s.findtext("latitude")),
        "longitude": parse_float(s.findtext("longitude")),
    }
    for f in FIELDS:
        raw = s.findtext(f)
        if f == "phenomenon":
            out[f] = (raw or "").strip() or None
        else:
            out[f] = parse_float(raw)
    return out


def extract_info_blocks(entry: ET.Element) -> list[dict]:
    """Return list of CAP <info> dicts from a MeteoAlarm ATOM entry.

    MeteoAlarm repeats the same alert in multiple languages — one CAP
    <info> block per language. We extract them all so the caller can
    pick the preferred language (English, to match WARN_EVENT_ET keys)
    while still checking areaDesc across translations."""
    infos: list[dict] = []
    for info_el in entry.iter():
        # Match both namespaced and bare <info> tags
        if not (info_el.tag.endswith("}info") or info_el.tag == "info"):
            continue
        block: dict[str, str] = {}
        # CAP <language> sits inside <info>; pull it out for preference logic
        for child in info_el:
            tag = child.tag.split("}")[-1]
            if child.text and child.text.strip():
                block.setdefault(tag, child.text.strip())
        # areaDesc sits inside a nested <area> block — grab the first one
        for area_el in info_el.iter():
            if area_el.tag.endswith("}area") or area_el.tag == "area":
                for ac in area_el:
                    ac_tag = ac.tag.split("}")[-1]
                    if ac_tag == "areaDesc" and ac.text:
                        block["areaDesc"] = ac.text.strip()
                break
        infos.append(block)
    return infos


def pick_preferred_info(infos: list[dict]) -> dict:
    """Pick the English info block if present, else Estonian, else first.
    This makes `event` come out as 'Thunderstorms Level 1' rather than
    'Äike Tase 1', so the app's WARN_EVENT_ET lookup table works."""
    for lang_pref in ("en-GB", "en-US", "en"):
        for info in infos:
            if info.get("language", "").startswith(lang_pref):
                return info
    for info in infos:
        if info.get("language", "").startswith("et"):
            return info
    return infos[0] if infos else {}


def _fetch_meteoalarm_xml(url: str) -> bytes:
    # MeteoAlarm rejects a strict Accept header with 406; use */*
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; MadiseIlmaradar/1.0; "
                "+https://github.com/indrekraag/wa3)"
            ),
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def _parse_meteoalarm(xml_bytes: bytes, country: str, now, future_cutoff,
                      seen: set) -> list:
    """Parse one country's MeteoAlarm ATOM feed into active-warning dicts,
    each tagged with `country`. `seen` dedups identifiers across feeds.
    Raises ET.ParseError on malformed XML (caller skips that feed)."""
    ns = {
        "a": "http://www.w3.org/2005/Atom",
        "cap": "urn:oasis:names:tc:emergency:cap:1.2",
    }
    root = ET.fromstring(xml_bytes)

    out: list[dict] = []
    for entry in root.findall("a:entry", ns):
        infos = extract_info_blocks(entry)

        # Fallback: if no structured <info> blocks were found, do the
        # original flat scrape so we don't lose alerts in unexpected
        # feed shapes.
        if not infos:
            flat = {}
            for el in entry.iter():
                if "cap" in el.tag and el.text and el.text.strip():
                    tag = el.tag.split("}")[-1]
                    flat.setdefault(tag, el.text.strip())
            if not flat:
                continue
            infos = [flat]

        chosen = pick_preferred_info(infos)

        # No area filter — every county/region in the feed is kept.

        # Expiry filter — drop if expired
        expires = parse_iso(chosen.get("expires"))
        if expires and expires <= now:
            continue

        # Future filter — drop if onset is too far ahead
        onset = parse_iso(chosen.get("onset"))
        if onset and onset > future_cutoff:
            continue

        # Identifier may only exist on the parent <alert> wrapper, not
        # inside <info>. Walk the entry to find it for dedup.
        ident = chosen.get("identifier") or ""
        if not ident:
            for el in entry.iter():
                if el.tag.endswith("}identifier") or el.tag == "identifier":
                    if el.text:
                        ident = el.text.strip()
                        break
        if ident and ident in seen:
            continue
        if ident:
            seen.add(ident)

        # chosen prefers the English info block (so `event` matches the
        # app's translation table); its areaDesc is the region name.
        area = chosen.get("areaDesc") or ""

        out.append({
            "event":     chosen.get("event"),
            "severity":  chosen.get("severity"),
            "areaDesc":  area,
            "onset":     chosen.get("onset"),
            "expires":   chosen.get("expires"),
            "urgency":   chosen.get("urgency"),
            "certainty": chosen.get("certainty"),
            "country":   country,
        })
    return out


def fetch_warnings() -> list:
    """Pull every configured MeteoAlarm country feed (EE + FI) and return
    all currently-active CAP warnings, each tagged with its country.

    Filters applied at fetch time:
      • expires must be in the future
      • onset must be within WARNING_LOOKAHEAD_HOURS from now (so
        multi-day-ahead alerts don't clutter the dashboard)
      • duplicate identifiers are collapsed across feeds

    Returns [] on total failure — warnings are decorative, not critical,
    so a failure here never fails the workflow. A single feed erroring is
    logged and skipped; the other feeds still contribute."""
    now = dt.datetime.now(dt.timezone.utc)
    future_cutoff = now + dt.timedelta(hours=WARNING_LOOKAHEAD_HOURS)

    warnings: list[dict] = []
    seen: set[str] = set()
    for country, url in METEOALARM_FEEDS:
        try:
            xml_bytes = _fetch_meteoalarm_xml(url)
        except Exception as exc:  # noqa: BLE001 — soft-fail per feed
            print(f"MeteoAlarm {country} fetch failed: {exc}", file=sys.stderr)
            continue
        try:
            warnings.extend(
                _parse_meteoalarm(xml_bytes, country, now, future_cutoff, seen)
            )
        except ET.ParseError as exc:
            print(f"MeteoAlarm {country} parse failed: {exc}", file=sys.stderr)
            continue
    return warnings


def build_snapshot(xml_bytes: bytes, wmo_codes: list[str], warnings=None) -> dict:
    root = ET.fromstring(xml_bytes)
    xml_ts = root.get("timestamp")
    obs_time = None
    if xml_ts and xml_ts.isdigit():
        obs_time = dt.datetime.fromtimestamp(int(xml_ts), tz=dt.timezone.utc).isoformat()

    stations = {}
    missing = []
    for wmo in wmo_codes:
        s = extract_station(root, wmo)
        if s is None:
            missing.append(wmo)
            continue
        d = station_to_dict(s)
        stations[wmo] = d

    snapshot = {
        "source": "EMHI / Estonian Weather Service",
        "source_url": EMHI_URL,
        "observation_time": obs_time,
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "stations": stations,
        "warnings": fetch_warnings() if warnings is None else warnings,
        "warnings_source": "MeteoAlarm / Estonian Environment Agency CAP feed",
        "warnings_scope": "all-Estonia + all-Finland (MeteoAlarm EE+FI, tagged per warning)",
    }
    if missing:
        snapshot["missing_wmo_codes"] = missing
    return snapshot


def set_action_output(name: str, value: str) -> None:
    """Expose a step output when running under GitHub Actions (no-op
    locally). The workflow gates its push step on ``wrote == 'true'`` so a
    soft failure leaves the data branch untouched."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(f"{name}={value}\n")


def fetch_snapshot_with_retries(wmo_codes: list[str], attempts: int = FETCH_ATTEMPTS):
    """Fetch + parse the EMHI XML, retrying transient failures.

    A single attempt must clear every transient hazard: the HTTP fetch
    (timeouts, 5xx), the XML parse (Cloudflare serves an HTML challenge
    page that won't parse), and a sanity check that at least one requested
    station is present (a truncated response is treated as a miss and
    retried). MeteoAlarm warnings are fetched once, only after EMHI
    succeeds, so a retried EMHI fetch doesn't hammer the warnings feed.

    Returns the assembled snapshot dict, or None if every attempt failed."""
    last_err = None
    for i in range(1, attempts + 1):
        try:
            xml_bytes = fetch_xml(EMHI_URL)
            # warnings=[] skips the MeteoAlarm fetch during validation.
            snap = build_snapshot(xml_bytes, wmo_codes, warnings=[])
            if not snap["stations"]:
                raise RuntimeError(
                    "response had none of the target stations "
                    f"(missing {snap.get('missing_wmo_codes')})"
                )
        except Exception as exc:  # noqa: BLE001 — catch-all is intentional for retry
            last_err = exc
            print(f"EMHI fetch attempt {i}/{attempts} failed: {exc}", file=sys.stderr)
            if i < attempts:
                delay = RETRY_BACKOFF_SECONDS[min(i - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
                print(f"  retrying in {delay}s…", file=sys.stderr)
                time.sleep(delay)
            continue
        # EMHI is good — warnings are decorative and soft-fail to [].
        snap["warnings"] = fetch_warnings()
        return snap

    print(
        f"EMHI fetch failed after {attempts} attempts; last error: {last_err}",
        file=sys.stderr,
    )
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/emhi.json")
    parser.add_argument(
        "--stations",
        nargs="+",
        default=DEFAULT_STATIONS,
        help="WMO codes (default: 26124 Lääne-Nigula, 26123 Haapsalu)",
    )
    args = parser.parse_args()

    snap = fetch_snapshot_with_retries(args.stations)
    if snap is None:
        # Soft failure: every attempt hit a transient error. Write nothing
        # and tell the workflow not to push, so the data branch keeps its
        # last good snapshot. Exit 0 so the cron doesn't email on a blip.
        print(
            "Soft failure: EMHI unreachable after retries — keeping the "
            "previous snapshot (no file written, no push)."
        )
        set_action_output("wrote", "false")
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"Wrote {out_path} — observation_time={snap['observation_time']}, "
        f"stations={list(snap['stations'])}, warnings={len(snap['warnings'])}"
    )
    set_action_output("wrote", "true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
