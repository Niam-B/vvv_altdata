# vvv-altdata

## Goal
Store-level alternative data on Valvoline Inc. (NYSE: VVV) to test a short thesis:
**same-store transactions are falling while price-led comps rise.** Fiscal year ends Sep 30.

## Data sources
- **store.vioc.com**
  - `index.html` lists state links `/{st}`; state pages list city links `/{st}/{city}`.
  - City pages list each store: name, address, store URL, live wait time
    ("0-10 Minutes" / "10-20 Minutes" / "20+ Minutes"), hours text, services.
  - Store pages `/{st}/{city}/{address-slug}`: Google rating + review count, recent dated
    reviews, coupons (amount, offer, expiry, "new customers only"), hours by day, lat/lon in
    meta tags, Google Place ID inside the review link.
- **jobs.vioc.com/service-center-jobs**: "Showing 1-20 of N jobs", 20 per page at
  `/service-center-jobs/page/{n}`; some listings tagged VIOCF.
- **Breeze / Oil Changers** stores were acquired Dec 1 2025 — TODO: flag them in stores.csv
  (not done yet) so comps can be computed ex-acquisition.

## Layout
- `collectors/http.py` – shared fetcher (~1 req/s + jitter, tenacity backoff, browser UA,
  Playwright fallback when static HTML lacks data). Imported as `collectors.http`; each
  script has a bootstrap block that strips `collectors/` from `sys.path` because a module
  named `http` would otherwise shadow the stdlib.
- `collectors/parse.py` – all HTML parsing (URL-structure / JSON-LD / regex based, written
  without live access — verify against real pages with `SAVE_RAW=1`).
- `collectors/vioc_master.py` → `data/ref/cities.csv`, `data/ref/stores.csv` (committed).
- `collectors/wait_times.py` → `data/live/wait_times/YYYY-MM-DD.csv` (every 30 min,
  12:00–02:30 UTC). Uses fixed sample `data/ref/wait_sample.csv` if a full pass > 20 min.
- `collectors/coupons_daily.py` → `data/live/coupons/YYYY-MM-DD.csv` (daily 14:15 UTC,
  fixed 400-store sample `data/ref/coupon_sample.csv`).
- `collectors/jobs_daily.py` → `data/live/jobs/YYYY-MM-DD.csv` (daily 14:15 UTC).
- `scripts/load_sqlite.py` → `data/db/vvv.sqlite` (local analysis only; rebuilt from CSVs).

## Rules
- Collectors run on GitHub Actions (no persistent disk): **append-only daily CSVs** in
  `data/live/`, dates in UTC. Never rewrite past files. SQLite is local only.
- Samples (`wait_sample.csv`, `coupon_sample.csv`) are fixed once created — don't regenerate
  them or the panel breaks. Delete deliberately only if the store universe changes a lot.
- Never commit `.env`, raw HTML (`data/raw/`), or `data/db/`.
- Keep request rate ~1/sec.

## Commands
```bash
pip install -r requirements.txt && python -m playwright install chromium
STATES=ky SAVE_RAW=1 python collectors/vioc_master.py   # small test crawl
python collectors/wait_times.py
python collectors/coupons_daily.py
python collectors/jobs_daily.py
python scripts/load_sqlite.py
python -m unittest discover tests
```
