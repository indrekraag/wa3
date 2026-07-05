# Ilmaradar (wa3) — live-location weather PWA

Personal PWA weather screen that **follows the phone's current location**
(GPS). One self-contained HTML file served by GitHub Pages, installable to
iPhone/Android home screens as a fullscreen web app.

This is a fork of **wa2** (the fixed-location "Madise Ilmaradar",
https://github.com/indrekraag/weatherapp2). wa3 is the same app with two
differences:

1. **Live GPS location** instead of a hardcoded house — the app asks the
   browser for the phone's position and shows weather wherever you are.
2. **No alternative weather stations** — wa2's three nearby-station chips
   (Kurevere / Lääne-Nigula / Haapsalu) are removed.

Everything else (forecast, radar, sun/moon, pollen, aurora, electricity
price, warnings, the whole visual design) is inherited from wa2.

- **Repo:** https://github.com/indrekraag/wa3
- **Live:** https://indrekraag.github.io/wa3/
- **Local folder:** `/Users/indrekraag/wa3/`

**Read this file in full at the start of every session before doing
anything else.** Update it whenever something non-obvious changes.

---

## Session start / end procedure

Start: read this file → `git status` → `git pull` → summarise **Current
state** back → ask what we're working on before changing anything.

End: `git status` → update **Recent changes** / **Current state** below →
`git add -A && git commit -m "…" && git push` → confirm the push succeeded.

---

## How the live location works

`CONFIG.lat` / `CONFIG.lng` (in `index.html`) are the single source of
truth for position. Almost every fetch/render reads them **at call time**,
so relocating just means updating them and re-running the pipeline.

- **Seed:** on load, `seedLocationFromCache()` reads the last-known fix
  from `localStorage['wx.geo']` so the app renders instantly and works
  offline. If there's no cached fix, `CONFIG` falls back to Madise coords
  (58.81627, 23.74447) purely as a placeholder until GPS resolves.
- **Update:** `initGeolocation()` calls `navigator.geolocation.getCurrentPosition`
  on **page load** and on **manual refresh** (the ↻ button). On success,
  `applyLocation(lat, lng)` updates `CONFIG`, persists to `wx.geo`, and
  re-runs the position-dependent fetches: `fetchWeatherAndForecast`,
  `fetchPollen`, `fetchOvation`, `fetchSunTimes`, `calcMoon`, and
  re-centers the radar map + marker (`RADAR.marker`). Kp and solar-wind
  are global (not position-dependent) and stay on their own timers.
- **Label:** `reverseGeocode(lat, lng)` hits BigDataCloud's keyless,
  CORS-open `reverse-geocode-client` endpoint and writes the place name
  into the hero title (`Hetkeilm · <koht>`, id `hero-title-h2`). On any
  failure the title just stays `Hetkeilm` — never breaks.
- **Permission denied / unavailable:** the error callback is a no-op, so
  the app silently keeps the last-known / fallback location.
- **Not continuous:** we do *not* `watchPosition()` — position updates
  only on load and manual refresh. (If you ever want it to track while
  open, wire `watchPosition` into `initGeolocation`.)

Geolocation requires a **secure context** — GitHub Pages (https) and
`http://localhost` both qualify; a plain `http://<LAN-IP>` does **not**,
so on-phone LAN testing won't get a GPS fix (it'll show the fallback).

## What the app shows

All in Estonian, top-down: warning row → **Hetkeilm** (current: big temp,
condition, feels-like/frost/dew pills, 6 measurement tiles) → 24h forecast
(temp/rain/wind bars) → 7-day strip (tap a day → detail bottom sheet) →
radar (satellite/map, zoom presets, sun/moon/wind overlays) → Päike → Kuu
→ Õietolm (pollen) → Virmalised (aurora) → Elektri hind (electricity spot
price). See wa2's history for the design details — the render layer is
identical.

## Data sources (unchanged from wa2 except as noted)

- **Open-Meteo** `api.open-meteo.com/v1/forecast` (ECMWF IFS) — current +
  hourly + daily. Uses `CONFIG.lat/lng`.
- **Open-Meteo Air Quality** — pollen. Uses `CONFIG.lat/lng`.
- **NOAA SWPC** — Kp forecast, Ovation aurora grid, solar-wind Bz
  (Ovation probability is read at `round(CONFIG.lat/lng)`).
- **BigDataCloud** `api.bigdatacloud.net/data/reverse-geocode-client` —
  **new in wa3**: keyless, CORS-open reverse geocoding for the location
  label. Best-effort; failure is silent.
- **MeteoAlarm** Estonia CAP feed → **all-Estonia** warnings (wa3 drops
  wa2's Lääne-county filter). CORS-closed → GitHub Actions bridge.
- **Elering** Nord Pool spot price → `data/nps.json`. CORS-closed → same
  bridge. Converted to snt/kWh incl VAT client-side.
- **Local astronomy** — sun/moon rise-set + alt/az computed in-browser.

**Removed vs wa2:** the tarktee Kurevere road-station fetch and the EMHI
Lääne-Nigula/Haapsalu station fetch (and their three UI chips). The EMHI
GitHub-Actions bridge still runs — it feeds **warnings** and (via
`fetch_nps.py`) the **electricity price** — but its station data is no
longer displayed.

## Architecture

```
~/wa3/
├── index.html                  # single-file PWA (CSS + JS inline)
├── manifest.json               # PWA install metadata ("Ilmaradar")
├── sw.js                       # service worker (stale-while-revalidate shell)
├── icons/                      # PNG icons + generate_icons.py (PIL)
├── scripts/
│   ├── fetch_emhi.py           # bridge: EMHI stations (unused by UI) + all-EE CAP warnings → emhi.json
│   └── fetch_nps.py            # bridge: Elering Nord Pool spot price → nps.json
├── screenshots.py              # Playwright multi-viewport screenshotter
├── .github/workflows/emhi.yml  # cron */15 + dispatch + push; force-pushes emhi.json+nps.json to 'data' branch
└── CLAUDE.md                   # this file
```

**Data flow:** SW pre-caches the shell; `hydrateFromCache()` renders last
cached data on load; `fetch*` run in parallel; every response is cached in
`localStorage` (`wx.*`, 6 h TTL); each source re-fetches on its own timer.
The MeteoAlarm/Elering bridge runs in GitHub Actions every 15 min and
force-pushes a single rolling commit to a `data` orphan branch; the PWA
reads it via `raw.githubusercontent.com/indrekraag/wa3/data/…` (CORS-open).
The workflow uses `${{ github.repository }}`, so it targets wa3
automatically once pushed — no repo name is hardcoded.

## Conventions

- **Language:** Estonian throughout.
- **Style:** vanilla JS, XMLHttpRequest (not fetch), no framework, no
  build step, single file. Keep it that way.
- **Position:** always read `CONFIG.lat/lng` live; never re-hardcode a
  location. New position-dependent code must also be re-run from
  `applyLocation()`.
- **Refresh cadence:** weather 5 min, pollen 1 h, Kp 30 min, Ovation +
  solar-wind 5 min, warnings 5 min, nps 15 min, sun 30 min, moon 1 h,
  full reload 2 h. Geolocation: load + manual refresh only.
- **Offline:** keep `saveCache` + `hydrateFromCache` working across any
  fetch refactor.

## Local development

```bash
cd ~/wa3
python3 -m http.server 8123          # http://localhost:8123 (secure context → GPS works)
```

Playwright screenshots: `python3 screenshots.py` (needs a `.venv` with
playwright; not committed). To test the live-GPS path headlessly, grant a
mock geolocation + permission in the Playwright context.

Regenerate `data/emhi.json` / `data/nps.json` locally:
`python3 scripts/fetch_emhi.py` / `python3 scripts/fetch_nps.py` (both
gitignored on main; live copies live on the `data` branch).

Orphan-ID checker (run after any HTML/JS edit — a `byId('x')` on a missing
element throws and halts all later JS):

```bash
python3 -c "
import re
html = open('index.html').read()
refs = set(re.findall(r\"byId\(['\\\"]([a-zA-Z0-9-]+)['\\\"]\)\", html))
ids = set(re.findall(r' id=\"([a-zA-Z0-9-]+)\"', html))
print('orphans:', sorted(refs - ids))"
```

## Deployment

Push to `main` → GitHub Pages serves automatically (enable Pages →
"Deploy from branch: main /root" in repo settings the first time). Live at
https://indrekraag.github.io/wa3/. The `data` branch is created/maintained
by the Actions workflow and is never merged to main.

## What NOT to do

- Don't introduce a build step or framework — single-file vanilla only.
- Don't re-hardcode a location or bypass `CONFIG.lat/lng`.
- Don't commit `.venv/`, `shots/`, `data/`, `.DS_Store`, `__pycache__/`,
  or `index_backup_*.html` (all gitignored).
- Don't write to a DOM id that doesn't exist — it throws and halts the
  rest of the script. Run the orphan-ID checker after HTML edits.
- Don't send `Accept: application/atom+xml` to MeteoAlarm — it 406s. Use
  `Accept: */*`.
- Don't touch the sibling wa2 repo from here.

---

## Current state

**Status:** Initial fork from wa2 created 2026-07-05. Live-GPS wiring done,
three alt-station chips removed, warnings broadened to all-Estonia, raw
data URLs repointed to `indrekraag/wa3`, rebranded to "Ilmaradar".
Verified: both inline scripts `node --check` OK, orphan-ID check clean, no
dangling references to removed symbols, bridge scripts `py_compile` OK.

**Next step:** create the GitHub repo `indrekraag/wa3`, push `main`, enable
GitHub Pages, let the Actions workflow populate the `data` branch, then
smoke-test on the actual iPhone (grant location permission on first load).

**Open follow-ups:**
- App name "Ilmaradar" and the icon wordmark ("MADISE") are placeholders —
  rename if desired (icon wordmark lives in `icons/generate_icons.py`).
- Consider `watchPosition()` if you want it to track while open.
- All-Estonia warnings may be noisy; could reverse-geocode the county from
  GPS and filter to nearby counties instead.
