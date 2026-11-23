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
- **Breeze / Oil Changers** stores were acquired Dec 1 2025. `analysis/review_panel.py` flags
  them only by brand name (breeze|oil changers) or `data/ref/breeze_stores.csv` (store_url and/or
  place_id; e.g. the 164 Oil Changers locations — no list committed yet, so currently none are
  flagged: all store names are "Valvoline Instant Oil Change"). Do NOT use first-Wayback-capture
  dates: current store.vioc.com URLs were first archived Oct–Dec 2025 for most stores.
- **Review history (free only — no Apify or paid scrapers):** Wayback Machine CDX + archived
  store pages. Current store URLs only exist from ~Oct 2025; older history is under OLD
  store.vioc.com URLs: `/{st}/{city}/{street}-{zip}-{CODE}` (2011–2019) and
  `/{st}/{city}/oil-change-{code}.html` (2019–Sep 2025), linked by the per-store CODE.
- **Income:** Census ACS 5-yr B19013 by ZCTA. The Census Data API now returns "Missing Key"
  without a key, so `fetch_acs_income.py` falls back to the free table-based Summary File on
  www2.census.gov; it exits 1 if neither works.

## Layout
- `collectors/http.py` – shared fetcher (~1 req/s + jitter, tenacity backoff, browser UA,
  Playwright fallback when static HTML lacks data). Imported as `collectors.http`; each
  script has a bootstrap block that strips `collectors/` from `sys.path` because a module
  named `http` would otherwise shadow the stdlib.
- `collectors/parse.py` – all HTML parsing (URL-structure / JSON-LD / regex based, written
  without live access — verify against real pages with `SAVE_RAW=1`).
- `collectors/vioc_master.py` → `data/ref/cities.csv`, `data/ref/stores.csv` (committed).
- **Duplicate store URLs:** store.vioc.com lists ~270 stores under two URLs differing only by
  periods in the slug (`.../10115-rosedale-hwy` vs `.../10115-rosedale-hwy.`, `1205-s--w--hwy` vs
  `1205-s.-w.-hwy`). `collectors.common.dedupe_stores` merges rows sharing place_id, OR lat/lon
  (4 dp), OR period-free slug, keeping the URL with no trailing period / fewest periods.
  `stores.csv` is stored de-duplicated; removed URLs are in `data/ref/store_url_aliases.csv`
  (alias_url → store_url). wait_times, coupons_daily, competition and review_panel map URLs to
  canonical at run time (fixed sample files are not rewritten). `scripts/dedupe_stores.py`
  re-applies it in place.
- `collectors/wait_times.py` → `data/live/wait_times/YYYY-MM-DD.csv` (every 30 min,
  12:00–02:30 UTC). Uses fixed sample `data/ref/wait_sample.csv` if a full pass > 20 min.
- `collectors/coupons_daily.py` → `data/live/coupons/YYYY-MM-DD.csv` (daily 14:15 UTC,
  fixed 400-store sample `data/ref/coupon_sample.csv`).
- `collectors/jobs_daily.py` → `data/live/jobs/YYYY-MM-DD.csv` (daily 14:15 UTC).
- `collectors/wayback_reviews.py` → `data/hist/wayback_cdx.csv`, `data/hist/wayback_snapshots.csv`
  (one archived snapshot per store-quarter from 2019Q1 for the coupon sample; resumable).
- `scripts/fetch_acs_income.py` → `data/ref/zcta_income.csv` (workflow `income-refresh` also
  re-runs competition.py).
- `scripts/probe_old_vioc_urls.py` → `data/hist/old_url_candidates.csv` (archived pre-2025-10 URLs
  per VIOC host; workflow `old-url-probe`).
- `collectors/old_vioc_history.py` → `data/hist/old_url_captures.csv`, `old_store_codes.csv`
  (code → current store), `store_first_seen.csv` (first_seen_date = earliest archived old URL, a
  store-age proxy), `old_url_snapshots.csv` (review_count per store-quarter 2019Q1–2025Q3 for the
  coupon sample; OLD_HISTORY_ALL=1 for all). Workflow `old-vioc-history` (input max_minutes;
  resumable).
- `analysis/review_panel.py` → `data/hist/review_panel_quarterly.csv` (store-quarter panel:
  new reviews scaled to 91 days, usable if 45–183 day gap and count not falling) and
  `data/hist/review_yoy_summary.csv` (same-store YoY overall / income tercile / age bucket;
  age = first Wayback capture, a proxy). Calendar quarters + VVV fiscal quarter labels.
- `collectors/take5_master.py` (+ `collectors/parse_take5.py`) → `data/ref/take5_stores.csv`
  (store URLs /locations/{state-slug}/{city}/{digits}/; `state` = slug, `state_abbr` = 2-letter;
  city-page fallback if the sitemap gives < 1,000 stores)
  (one-off Take 5 crawl from www.take5.com/sitemap.xml; resumable, FRESH=1 restarts).
- `analysis/competition.py` → `data/derived/vvv_competition.csv` (per Valvoline store: Take 5
  within 1/3/5 mi, nearest Take 5, other Valvoline within 3 mi, ZIP income). Exit 1 on missing inputs.
- `scripts/load_sqlite.py` → `data/db/vvv.sqlite` (local analysis only; rebuilt from CSVs).
- Workflows: `master-crawl` (manual), `collect` (scheduled), `wayback-reviews` (manual,
  re-run until "remaining: 0"), `take5-crawl` (manual, one-off), `collect-loop` (scheduled), `income-refresh`,
  `old-url-probe`, `old-vioc-history` (manual).

## Rules
- Collectors run on GitHub Actions (no persistent disk): **append-only daily CSVs** in
  `data/live/`, dates in UTC. Never rewrite past files. SQLite is local only.
- Samples (`wait_sample.csv`, `coupon_sample.csv`) are fixed once created — don't regenerate
  them or the panel breaks. Delete deliberately only if the store universe changes a lot.
- Never commit `.env`, raw HTML (`data/raw/`), or `data/db/`.
- Keep request rate ~1/sec (Wayback: 1.5 s).
- Free data sources only; no API secrets in workflows.

## Commands
```bash
pip install -r requirements.txt && python -m playwright install chromium
STATES=ky SAVE_RAW=1 python collectors/vioc_master.py   # small test crawl
python collectors/wait_times.py
python collectors/coupons_daily.py
python collectors/jobs_daily.py
python collectors/wayback_reviews.py
python analysis/review_panel.py
python collectors/take5_master.py && python analysis/competition.py
python scripts/load_sqlite.py
python -m unittest discover tests
```
