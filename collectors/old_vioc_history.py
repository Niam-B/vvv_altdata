"""History of current VIOC stores under their pre-late-2025 store.vioc.com URLs (Wayback, free).

Old URL patterns on store.vioc.com (found with scripts/probe_old_vioc_urls.py):
  A  2011-2019  /{st}/{city}/{street-slug}-{zip}-{CODE}     e.g. /ny/tonawanda/2130-niagara-falls-blvd-14150-AQ5002
  B  2019-2025  /{st}/{city}/oil-change-{code}.html          e.g. /ca/hesperia/oil-change-gn0164.html
CODE is a per-store id (e.g. GN0164, 070239) that links A and B.

Phases (each resumable; outputs in data/hist/):
  1 captures   one CDX query per state prefix (before 2025-10-01) -> old_url_captures.csv
  2 resolve    map each old code to a current store (data/ref/stores.csv):
                 a. pattern A: zip + street number match the current address
                 b. otherwise fetch the latest archived B/A page for the code and match by
                    place_id, then lat/lon (<= 0.15 mi), then zip + street number
               -> old_store_codes.csv (store_url, place_id, code, match_method, ...)
  3 first seen -> store_first_seen.csv (store_url, place_id, old_url, first_seen_date, n_snapshots,
                 last_seen_date, code, match_method); first_seen_date is a store-age proxy
  4 reviews    one archived snapshot per store-quarter (2019Q1-2025Q3) of the old URLs, parsed for
               Google review_count -> old_url_snapshots.csv. Default: the coupon sample (same
               stores as the live data); OLD_HISTORY_ALL=1 for every store.

Env: OLD_HISTORY_MAX_MINUTES (default 320), WAYBACK_INTERVAL (default 1.5 s), OLD_HISTORY_ALL.
"""
# --- bootstrap: collectors/http.py would shadow stdlib `http`; import as a package
import sys, pathlib  # noqa: E401
_HERE = pathlib.Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if pathlib.Path(p or ".").resolve() != _HERE]
sys.path.insert(0, str(_HERE.parent))
# ---------------------------------------------------------------------------

import json
import logging
import math
import os
import re
import time
from collections import Counter, defaultdict
from urllib.parse import urlparse

from collectors.common import (
    DATA, REF, append_rows, dedupe_urls, load_stores, read_csv, setup_logging, ts_utc, write_csv,
)
from collectors.http import Fetcher
from collectors.parse import STREET_RE, parse_store

log = logging.getLogger("old_history")

HIST = DATA / "hist"
CAPTURES = HIST / "old_url_captures.csv"
CODES = HIST / "old_store_codes.csv"
FIRST_SEEN = HIST / "store_first_seen.csv"
SNAPS = HIST / "old_url_snapshots.csv"
CDX_API = "https://web.archive.org/cdx/search/cdx"
CUTOFF = "20250930"

CAP_FIELDS = ["state", "original", "timestamp", "pattern", "code"]
CODE_FIELDS = ["code", "store_url", "place_id", "match_method", "old_url_b", "old_url_a", "status"]
FS_FIELDS = ["store_url", "place_id", "old_url", "first_seen_date", "n_snapshots", "last_seen_date",
             "code", "match_method"]
SNAP_FIELDS = ["store_url", "quarter", "old_url", "wayback_ts", "snapshot_date", "review_count", "rating",
               "status", "fetched_ts"]

RE_A = re.compile(r"^/([a-z]{2})/([^/]+)/(.+)-(\d{5})-([a-z]{2}\d{3,5}|\d{6})$", re.I)
RE_B = re.compile(r"^/([a-z]{2})/([^/]+)/oil-change-([a-z0-9]+)\.html$", re.I)


def classify(original: str):
    """('A'|'B', code, zip, street_number) or None."""
    path = urlparse(original).path.rstrip("/")
    if m := RE_B.match(path):
        return "B", m.group(3).lower(), "", ""
    if m := RE_A.match(path):
        num = re.match(r"(\d+)", m.group(3))
        return "A", m.group(5).lower(), m.group(4), num.group(1) if num else ""
    return None


def zip_num(address: str) -> tuple[str, str]:
    z = re.findall(r"\b(\d{5})(?:-\d{4})?\b", address or "")
    n = re.match(r"\s*(\d+)", address or "")
    return (z[-1] if z else ""), (n.group(1) if n else "")


def miles(a, b) -> float:
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 2 * 3958.76 * math.asin(math.sqrt(min(1.0, h)))


def quarter_of(ts: str) -> str:
    return f"{ts[:4]}Q{(int(ts[4:6]) - 1) // 3 + 1}"


class Budget:
    def __init__(self, minutes: float):
        self.t0, self.limit = time.monotonic(), minutes * 60

    def out(self) -> bool:
        return time.monotonic() - self.t0 > self.limit


# ------------------------------------------------------------------ phase 1
def phase_captures(f: Fetcher, states: list[str], budget: Budget) -> list[dict]:
    done = {r["state"] for r in read_csv(CAPTURES)} if CAPTURES.exists() else set()
    for st in states:
        if st in done or budget.out():
            continue
        url = (f"{CDX_API}?url=store.vioc.com/{st}/&matchType=prefix&output=json&fl=original,timestamp"
               f"&filter=statuscode:200&collapse=timestamp:8&to={CUTOFF}&limit=500000")
        body = f.get(url)
        if body is None:
            log.warning("CDX failed for %s; will retry next run", st)
            continue
        try:
            data = json.loads(body) if body.strip() else []
        except ValueError:
            log.warning("bad CDX JSON for %s: %r", st, body[:200])
            continue
        rows = []
        for orig, ts in (data[1:] if data and data[0] == ["original", "timestamp"] else data):
            if c := classify(orig):
                rows.append({"state": st, "original": orig, "timestamp": ts, "pattern": c[0], "code": c[1]})
        if not rows:  # marker row so the state is not queried again
            rows = [{"state": st, "original": "", "timestamp": "", "pattern": "", "code": ""}]
        append_rows(CAPTURES, CAP_FIELDS, rows)
        log.info("captures %s: %d", st, len(rows))
    return [r for r in read_csv(CAPTURES) if r["code"]] if CAPTURES.exists() else []


# ------------------------------------------------------------------ phase 2
def phase_resolve(f: Fetcher, caps: list[dict], stores: list[dict], budget: Budget) -> dict[str, dict]:
    resolved = {r["code"]: r for r in read_csv(CODES)} if CODES.exists() else {}
    by_code: dict[str, list[dict]] = defaultdict(list)
    for c in caps:
        by_code[c["code"]].append(c)

    by_zipnum = defaultdict(list)
    by_pid = {}
    geo = []
    for s in stores:
        z, n = zip_num(s["address"])
        if z and n:
            by_zipnum[(z, n)].append(s)
        if s.get("place_id"):
            by_pid[s["place_id"]] = s
        try:
            geo.append(((float(s["lat"]), float(s["lon"])), s))
        except (TypeError, ValueError):
            pass

    def latest(rows, pattern):
        rows = [r for r in rows if r["pattern"] == pattern]
        return max(rows, key=lambda r: r["timestamp"]) if rows else None

    new = []
    for code, rows in sorted(by_code.items()):
        if code in resolved and resolved[code]["status"] in ("ok", "unmatched"):
            continue
        b, a = latest(rows, "B"), latest(rows, "A")
        rec = {"code": code, "store_url": "", "place_id": "", "match_method": "",
               "old_url_b": b["original"] if b else "", "old_url_a": a["original"] if a else "", "status": ""}
        # a. pattern A address match (no fetch needed)
        for r in rows:
            if r["pattern"] == "A":
                _, _, z, n = classify(r["original"])
                cand = by_zipnum.get((z, n), [])
                if len(cand) == 1:
                    rec.update(store_url=cand[0]["store_url"], place_id=cand[0].get("place_id", ""),
                               match_method="url_zip_streetnum", status="ok")
                    break
        # b. fetch the latest archived page and match on its contents
        if not rec["status"]:
            if budget.out():  # keep going: later codes may still match from their URL alone
                continue
            src = b or a
            html = f.get(f"https://web.archive.org/web/{src['timestamp']}id_/{src['original']}")
            if html is None:
                rec["status"] = "fetch_error"
            else:
                p = parse_store(html, src["original"])
                hit, how = None, ""
                if p["place_id"] and p["place_id"] in by_pid:
                    hit, how = by_pid[p["place_id"]], "page_place_id"
                if not hit and p["lat"] is not None and geo:
                    d, s = min(((miles((p["lat"], p["lon"]), g), s) for g, s in geo), key=lambda x: x[0])
                    if d <= 0.15:
                        hit, how = s, f"page_latlon_{d:.2f}mi"
                if not hit:
                    addr = p["address"] or (m.group(0) if (m := STREET_RE.search(html)) else "")
                    cand = by_zipnum.get(zip_num(addr), [])
                    if len(cand) == 1:
                        hit, how = cand[0], "page_zip_streetnum"
                if hit:
                    rec.update(store_url=hit["store_url"], place_id=hit.get("place_id", ""), match_method=how,
                               status="ok")
                else:
                    rec["status"] = "unmatched"
        resolved[code] = rec
        new.append(rec)
        if len(new) % 50 == 0:
            write_csv(CODES, CODE_FIELDS, sorted(resolved.values(), key=lambda r: r["code"]))
            log.info("resolved %d codes this run", len(new))
    write_csv(CODES, CODE_FIELDS, sorted(resolved.values(), key=lambda r: r["code"]))
    return resolved


# ------------------------------------------------------------------ phase 3
def phase_first_seen(caps: list[dict], resolved: dict, stores: list[dict]) -> list[dict]:
    codes_of = defaultdict(set)
    meta = {}
    for code, r in resolved.items():
        if r["status"] == "ok" and r["store_url"]:
            codes_of[r["store_url"]].add(code)
            meta.setdefault(r["store_url"], r)
    caps_by_code = defaultdict(list)
    for c in caps:
        caps_by_code[c["code"]].append(c)
    out = []
    for s in stores:
        su = s["store_url"]
        cs = [c for code in codes_of.get(su, ()) for c in caps_by_code[code]]
        days = sorted({c["timestamp"][:8] for c in cs})
        bs = [c for c in cs if c["pattern"] == "B"]
        old_url = max(bs or cs, key=lambda c: c["timestamp"])["original"] if cs else ""
        fmt = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d else ""  # noqa: E731
        m = meta.get(su, {})
        out.append({"store_url": su, "place_id": s.get("place_id", ""),
                    "old_url": urlparse(old_url)._replace(query="").geturl() if old_url else "",
                    "first_seen_date": fmt(days[0]) if days else "", "n_snapshots": len(days),
                    "last_seen_date": fmt(days[-1]) if days else "",
                    "code": "|".join(sorted(codes_of.get(su, ()))), "match_method": m.get("match_method", "")})
    write_csv(FIRST_SEEN, FS_FIELDS, out)
    return out


# ------------------------------------------------------------------ phase 4
def phase_reviews(f: Fetcher, caps: list[dict], resolved: dict, targets: set, budget: Budget) -> Counter:
    done = {(r["store_url"], r["quarter"]) for r in read_csv(SNAPS)
            if r["status"] in ("ok", "no_count")} if SNAPS.exists() else set()
    store_of = {code: r["store_url"] for code, r in resolved.items() if r["status"] == "ok"}
    per_sq: dict[tuple, dict] = {}
    for c in caps:
        su = store_of.get(c["code"])
        if su in targets and c["timestamp"][:4] >= "2019":
            k = (su, quarter_of(c["timestamp"]))
            if k not in per_sq or c["timestamp"] > per_sq[k]["timestamp"]:  # latest in quarter
                per_sq[k] = c
    st = Counter()
    for (su, q), c in sorted(per_sq.items()):
        if (su, q) in done:
            continue
        if budget.out():
            st["stopped_by_budget"] = 1
            break
        html = f.get(f"https://web.archive.org/web/{c['timestamp']}id_/{c['original']}")
        ts = c["timestamp"]
        row = {"store_url": su, "quarter": q, "old_url": c["original"], "wayback_ts": ts,
               "snapshot_date": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}", "fetched_ts": ts_utc()}
        if html is None:
            row["status"] = "fetch_error"
        else:
            p = parse_store(html, c["original"])
            row.update(review_count="" if p["review_count"] is None else p["review_count"],
                       rating="" if p["rating"] is None else p["rating"],
                       status="ok" if p["review_count"] is not None else "no_count")
        st[row["status"]] += 1
        append_rows(SNAPS, SNAP_FIELDS, [row])
    st["remaining"] = sum(1 for k in per_sq if k not in done) - sum(v for k, v in st.items()
                                                                   if k in ("ok", "no_count"))
    return st


def main() -> int:
    setup_logging()
    budget = Budget(float(os.environ.get("OLD_HISTORY_MAX_MINUTES", "320")))
    f = Fetcher(min_interval=float(os.environ.get("WAYBACK_INTERVAL", "1.5")), jitter=0.5, timeout=90,
                use_playwright=False)
    stores, alias = load_stores()
    states = sorted({s["state"].lower() for s in stores})

    caps = phase_captures(f, states, budget)
    print(f"phase 1: {len(caps)} old-URL captures, {len({c['code'] for c in caps})} store codes, "
          f"states done {len({r['state'] for r in read_csv(CAPTURES)}) if CAPTURES.exists() else 0}/{len(states)}")
    if not caps:
        print("ERROR: no old-URL captures found")
        return 1

    resolved = phase_resolve(f, caps, stores, budget)
    sc = Counter(r["status"] for r in resolved.values())
    mm = Counter(re.sub(r"_[\d.]+mi$", "", r["match_method"]) for r in resolved.values() if r["status"] == "ok")
    print(f"phase 2: codes {len(resolved)}  status {dict(sc)}  methods {dict(mm)}")

    fs = phase_first_seen(caps, resolved, stores)
    with_old = [r for r in fs if r["first_seen_date"]]
    yrs = Counter(r["first_seen_date"][:4] for r in with_old)
    print(f"phase 3: stores with an old URL: {len(with_old)}/{len(fs)} "
          f"({100 * len(with_old) / max(1, len(fs)):.1f}%)  first_seen year: {dict(sorted(yrs.items()))}")

    if os.environ.get("OLD_HISTORY_ALL") == "1":
        targets = {s["store_url"] for s in stores}
    else:
        targets = set(dedupe_urls((r["store_url"] for r in read_csv(REF / "coupon_sample.csv")), alias))
    st = phase_reviews(f, caps, resolved, targets, budget)
    rows = read_csv(SNAPS) if SNAPS.exists() else []
    ok = [r for r in rows if r["status"] == "ok"]
    print(f"phase 4 ({len(targets)} target stores): this run {dict(st)}; total snapshots {len(rows)}, "
          f"with review_count {len(ok)}, stores with >=1 count {len({r['store_url'] for r in ok})}")
    print(f"requests: {f.stats}  elapsed {(time.monotonic() - budget.t0) / 60:.1f} min"
          + ("  [time budget hit — re-run to continue]" if budget.out() else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
