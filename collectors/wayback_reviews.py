"""Historical Google review counts from Wayback Machine snapshots of store pages.

Free only: Internet Archive CDX API + archived pages (no API key).

For each store in the fixed sample (default data/ref/coupon_sample.csv, the same
stores whose live review_count is collected daily):
  1. CDX lookup of all 200-status captures of the store page (any year) ->
     data/hist/wayback_cdx.csv  (store_url, n_captures, first_capture, last_capture, cdx_status)
     first_capture is also used as a store-age proxy; quarter_caps caches the chosen
     capture per quarter so re-runs skip the CDX call.
  2. One capture per calendar quarter from 2019Q1 on (the latest capture in the quarter),
     fetched raw via /web/{ts}id_/{url} and parsed with collectors.parse.parse_store ->
     data/hist/wayback_snapshots.csv
     (store_url, quarter, wayback_ts, snapshot_date, review_count, rating, status, fetched_ts)

Resumable: stores/quarters already present are skipped, so the workflow can be re-run
until the log says "remaining: 0". Stops cleanly after WAYBACK_MAX_MINUTES.

Env: WAYBACK_SAMPLE (csv with store_url), WAYBACK_MAX_MINUTES (default 320),
     WAYBACK_FROM (default 2019), WAYBACK_INTERVAL (seconds between requests, default 1.5)
"""
# --- bootstrap: collectors/http.py would shadow stdlib `http`; import as a package
import sys, pathlib  # noqa: E401
_HERE = pathlib.Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if pathlib.Path(p or ".").resolve() != _HERE]
sys.path.insert(0, str(_HERE.parent))
# ---------------------------------------------------------------------------

import json
import logging
import os
import re
import time
from collections import Counter
from urllib.parse import quote, urlparse

from collectors.common import DATA, REF, append_rows, read_csv, setup_logging, ts_utc
from collectors.http import Fetcher
from collectors.parse import parse_store

log = logging.getLogger("wayback")

HIST = DATA / "hist"
CDX_FIELDS = ["store_url", "n_captures", "first_capture", "last_capture", "cdx_status", "quarter_caps"]
SNAP_FIELDS = ["store_url", "quarter", "wayback_ts", "snapshot_date", "review_count", "rating",
               "status", "fetched_ts"]
CDX_API = "https://web.archive.org/cdx/search/cdx"


def norm_path(u: str) -> str:
    p = urlparse(u if "://" in u else "http://" + u)
    return re.sub(r"(/index)?\.html?$", "", p.path).rstrip("/").lower()


def quarter_of(ts: str) -> str:
    return f"{ts[:4]}Q{(int(ts[4:6]) - 1) // 3 + 1}"


def cdx_captures(f: Fetcher, store_url: str) -> list[tuple[str, str]] | None:
    """All daily-collapsed 200 captures [(timestamp, original)] for exactly this page."""
    target = norm_path(store_url)
    key = store_url.split("://", 1)[-1]
    url = (f"{CDX_API}?url={quote(key, safe='/')}&matchType=prefix&output=json"
           f"&fl=timestamp,original&filter=statuscode:200&collapse=timestamp:8")
    body = f.get(url)
    if body is None:
        return None
    try:
        rows = json.loads(body) if body.strip() else []
    except ValueError:
        log.warning("bad CDX json for %s: %r", store_url, body[:200])
        return None
    out = [(ts, orig) for ts, orig in rows[1:] if norm_path(orig) == target]
    return sorted(out)


def main() -> int:
    setup_logging()
    t0 = time.monotonic()
    max_min = float(os.environ.get("WAYBACK_MAX_MINUTES", "320"))
    start_year = os.environ.get("WAYBACK_FROM", "2019")
    sample_path = pathlib.Path(os.environ.get("WAYBACK_SAMPLE", REF / "coupon_sample.csv"))
    if not sample_path.exists():
        log.error("%s not found — run master-crawl first", sample_path)
        return 1
    stores = sorted({r["store_url"] for r in read_csv(sample_path)})

    cdx_path, snap_path = HIST / "wayback_cdx.csv", HIST / "wayback_snapshots.csv"
    cdx_done = {r["store_url"]: r for r in read_csv(cdx_path)} if cdx_path.exists() else {}
    cdx_done = {k: v for k, v in cdx_done.items() if v["cdx_status"] == "ok"}  # retry failures
    snaps_done = set()
    if snap_path.exists():
        snaps_done = {(r["store_url"], r["quarter"]) for r in read_csv(snap_path)
                      if r["status"] in ("ok", "no_count")}

    f = Fetcher(min_interval=float(os.environ.get("WAYBACK_INTERVAL", "1.5")), jitter=0.5,
                timeout=60, use_playwright=False)
    st = Counter()
    out_of_time = False
    for i, su in enumerate(stores, 1):
        if time.monotonic() - t0 > max_min * 60:
            out_of_time = True
            break
        if su in cdx_done:  # cached from an earlier run: no CDX request needed
            per_q = {q: tuple(v) for q, v in json.loads(cdx_done[su].get("quarter_caps") or "{}").items()}
        else:
            caps = cdx_captures(f, su)
            if caps is None:
                append_rows(cdx_path, CDX_FIELDS, [{"store_url": su, "cdx_status": "error"}])
                st["cdx_error"] += 1
                continue
            # latest capture in each quarter from start_year on
            per_q = {}
            for ts, orig in caps:
                if ts[:4] >= start_year:
                    per_q[quarter_of(ts)] = (ts, orig)
            append_rows(cdx_path, CDX_FIELDS, [{
                "store_url": su, "n_captures": len(caps),
                "first_capture": caps[0][0] if caps else "", "last_capture": caps[-1][0] if caps else "",
                "cdx_status": "ok", "quarter_caps": json.dumps(per_q, separators=(",", ":"))}])
        for q, (ts, orig) in sorted(per_q.items()):
            if (su, q) in snaps_done:
                continue
            if time.monotonic() - t0 > max_min * 60:
                out_of_time = True
                break
            html = f.get(f"https://web.archive.org/web/{ts}id_/{orig}")
            row = {"store_url": su, "quarter": q, "wayback_ts": ts,
                   "snapshot_date": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}", "fetched_ts": ts_utc()}
            if html is None:
                row["status"] = "fetch_error"
            else:
                rec = parse_store(html, su)
                row.update(review_count=rec["review_count"] if rec["review_count"] is not None else "",
                           rating=rec["rating"] if rec["rating"] is not None else "",
                           status="ok" if rec["review_count"] is not None else "no_count")
            st[row["status"]] += 1
            append_rows(snap_path, SNAP_FIELDS, [row])
        if out_of_time:
            break
        if i % 25 == 0:
            log.info("stores %d/%d  %s", i, len(stores), dict(st))

    # remaining work estimate for the log
    done_stores = len({r["store_url"] for r in read_csv(cdx_path) if r["cdx_status"] == "ok"}) \
        if cdx_path.exists() else 0
    print("\n=== wayback reviews ===")
    print(f"this run: {dict(st)}  requests: {f.stats}  elapsed: {(time.monotonic() - t0) / 60:.1f} min")
    print(f"stores with CDX done: {done_stores}/{len(stores)}  remaining: {len(stores) - done_stores}"
          + ("  [stopped by WAYBACK_MAX_MINUTES — re-run the workflow to continue]" if out_of_time else ""))
    if snap_path.exists():
        rows = read_csv(snap_path)
        ok = [r for r in rows if r["status"] == "ok"]
        print(f"snapshots total: {len(rows)}  with review_count: {len(ok)} "
              f"({100 * len(ok) / max(1, len(rows)):.0f}%)  stores with >=1 count: "
              f"{len({r['store_url'] for r in ok})}")
        if rows and not ok:
            print("WARNING: no archived page yielded a review_count — reviews may be JS-loaded in the "
                  "archived HTML; inspect a snapshot and adjust collectors/parse.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
