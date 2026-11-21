"""Daily snapshot of service-center job postings on jobs.vioc.com.

Pages through /service-center-jobs/page/{n} (20 per page) until N is covered.
Appends one row per listing to data/live/jobs/YYYY-MM-DD.csv:
  ts_utc, total_jobs, page, job_url, title, address, label
label is a |-joined set of tags found on the card (e.g. "VIOCF").
"""
# --- bootstrap: collectors/http.py would shadow stdlib `http`; import as a package
import sys, pathlib  # noqa: E401
_HERE = pathlib.Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if pathlib.Path(p or ".").resolve() != _HERE]
sys.path.insert(0, str(_HERE.parent))
# ---------------------------------------------------------------------------

import logging
import math
import os
import time

from collectors.common import JOBS_BASE, append_rows, daily_csv, setup_logging, ts_utc
from collectors.http import Fetcher
from collectors.parse import jobs_has_data, parse_jobs, parse_jobs_total

log = logging.getLogger("jobs")

FIELDS = ["ts_utc", "total_jobs", "page", "job_url", "title", "address", "label"]
PER_PAGE = 20


def page_url(n: int) -> str:
    return f"{JOBS_BASE}/service-center-jobs" if n == 1 else f"{JOBS_BASE}/service-center-jobs/page/{n}"


def main() -> int:
    setup_logging()
    t0 = time.monotonic()
    max_min = float(os.environ.get("JOBS_MAX_MINUTES", "5"))
    f = Fetcher()
    ts = ts_utc()

    html = f.get(page_url(1), has_data=jobs_has_data)
    total = parse_jobs_total(html) if html else None
    if total is None:
        log.error("could not read 'Showing 1-20 of N jobs' on page 1 — parser needs updating")
        return 1
    pages = max(1, math.ceil(total / PER_PAGE))
    log.info("total jobs N=%d across %d pages", total, pages)

    seen: set = set()
    out_path = daily_csv("jobs")
    n_rows = 0
    viocf = 0
    for p in range(1, pages + 1):
        if p > 1:
            if time.monotonic() - t0 > max_min * 60:
                log.warning("time budget hit at page %d/%d", p, pages)
                break
            html = f.get(page_url(p), has_data=jobs_has_data)
            if html is None:
                continue
        rows = []
        for j in parse_jobs(html, page_url(p)):
            if j["job_url"] in seen:
                continue
            seen.add(j["job_url"])
            viocf += "VIOCF" in j["label"]
            rows.append({"ts_utc": ts, "total_jobs": total, "page": p, **j})
        if not rows:
            log.warning("no listings parsed on page %d", p)
        n_rows += append_rows(out_path, FIELDS, rows)

    if n_rows == 0:  # still record N so the headline series never has gaps
        n_rows = append_rows(out_path, FIELDS, [{"ts_utc": ts, "total_jobs": total, "page": 0}])
    print(f"\n=== jobs: N={total}, {len(seen)} listings parsed ({viocf} VIOCF) -> {out_path} ===")
    print(f"requests: {f.stats}  elapsed: {(time.monotonic() - t0) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
