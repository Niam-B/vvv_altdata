"""Paths, CSV helpers and deterministic stratified sampling shared by collectors."""
from __future__ import annotations

import csv
import logging
import random
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
REF = DATA / "ref"
LIVE = DATA / "live"

BASE = "https://store.vioc.com"
JOBS_BASE = "https://jobs.vioc.com"
SAMPLE_SEED = 20251201  # fixed: samples must be identical across runs


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ts_utc(dt: datetime | None = None) -> str:
    return (dt or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def daily_csv(kind: str, dt: datetime | None = None) -> Path:
    """data/live/<kind>/YYYY-MM-DD.csv (UTC date)."""
    return LIVE / kind / f"{(dt or utc_now()).strftime('%Y-%m-%d')}.csv"


def append_rows(path: Path, fieldnames: Sequence[str], rows: Iterable[dict]) -> int:
    """Append rows to a CSV, writing the header only when the file is new. Append-only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    n = 0
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if new:
            w.writeheader()
        for row in rows:
            w.writerow(row)
            n += 1
    return n


def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    n = 0
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)
            n += 1
    tmp.replace(path)
    return n


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def stratified_sample(items: list[dict], key: str, n: int, seed: int = SAMPLE_SEED) -> list[dict]:
    """Deterministic proportional-allocation sample of ``n`` items stratified by ``key``.

    Every stratum with at least one item gets >= 1 pick (largest-remainder allocation).
    """
    if n >= len(items):
        return list(items)
    groups: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        groups[it[key]].append(it)
    total = len(items)
    quotas = {k: max(1, int(n * len(v) / total)) for k, v in groups.items()}
    # adjust to hit n exactly using largest remainders
    remainders = sorted(groups, key=lambda k: (n * len(groups[k]) / total) - int(n * len(groups[k]) / total), reverse=True)
    i = 0
    while sum(quotas.values()) < n and remainders:
        k = remainders[i % len(remainders)]
        if quotas[k] < len(groups[k]):
            quotas[k] += 1
        i += 1
    while sum(quotas.values()) > n:
        k = max(quotas, key=lambda k: quotas[k])
        quotas[k] -= 1
    rng = random.Random(seed)
    out: list[dict] = []
    for k in sorted(groups):
        g = sorted(groups[k], key=lambda r: tuple(str(v) for v in r.values()))
        out.extend(rng.sample(g, min(quotas[k], len(g))))
    return out


# ---------------------------------------------------------------- store de-duplication
# store.vioc.com lists some stores under two URLs whose address slugs differ only by periods
# (".../10115-rosedale-hwy" vs ".../10115-rosedale-hwy.", ".../1205-s--w--hwy" vs
# ".../1205-s.-w.-hwy"). Rows are the same store if they share a place_id, OR lat/lon rounded
# to 4 dp (~11 m), OR the URL path with periods removed and repeated hyphens collapsed.
ALIASES_CSV = "store_url_aliases.csv"  # data/ref/: alias_url -> store_url (canonical)
ALIAS_FIELDS = ["alias_url", "store_url"]


def _url_norm(url: str) -> str:
    path = url.split("://", 1)[-1].split("/", 1)[-1].lower().rstrip("/")
    return re.sub(r"-+", "-", path.replace(".", ""))


def _slug(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _url_rank(url: str) -> tuple:
    """Lower is preferred: no trailing period, fewest periods, shortest, then alphabetical."""
    s = _slug(url)
    return (s.endswith("."), s.count("."), len(url), url)


def _latlon_key(row: dict):
    try:
        lat, lon = float(row.get("lat") or "nan"), float(row.get("lon") or "nan")
    except (TypeError, ValueError):
        return None
    if lat != lat or lon != lon:  # NaN
        return None
    return f"{lat:.4f},{lon:.4f}"


def dedupe_stores(rows: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Collapse duplicate store rows.

    Returns (kept_rows, alias_map) where alias_map maps EVERY input store_url to its canonical
    store_url. The kept row is the preferred URL's row, with empty fields filled from its
    duplicates. Order of first appearance is preserved.
    """
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first: dict = {}
    for i, r in enumerate(rows):
        keys = [("url", _url_norm(r["store_url"]))]
        if (r.get("place_id") or "").strip():
            keys.append(("pid", r["place_id"].strip()))
        if (ll := _latlon_key(r)) is not None:
            keys.append(("ll", ll))
        for k in keys:
            if k in first:
                parent[find(i)] = find(first[k])
            else:
                first[k] = i

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(rows)):
        groups[find(i)].append(i)

    alias: dict[str, str] = {}
    keep_idx: dict[int, dict] = {}
    for members in groups.values():
        best = min(members, key=lambda i: _url_rank(rows[i]["store_url"]))
        row = dict(rows[best])
        for i in sorted(members, key=lambda i: _url_rank(rows[i]["store_url"])):
            for k, v in rows[i].items():
                if row.get(k) in (None, "") and v not in (None, ""):
                    row[k] = v
        for i in members:
            alias[rows[i]["store_url"]] = row["store_url"]
        keep_idx[min(members)] = row
    return [keep_idx[i] for i in sorted(keep_idx)], alias


def load_aliases() -> dict[str, str]:
    p = REF / ALIASES_CSV
    return {r["alias_url"]: r["store_url"] for r in read_csv(p)} if p.exists() else {}


def load_stores() -> tuple[list[dict], dict[str, str]]:
    """De-duplicated data/ref/stores.csv plus a url -> canonical url map (incl. saved aliases)."""
    rows, alias = dedupe_stores(read_csv(REF / "stores.csv"))
    saved = load_aliases()
    return rows, {**saved, **alias}


def canonical_url(url: str, alias: dict[str, str]) -> str:
    return alias.get(url, url)


def store_url_key(url: str) -> str:
    """Period/hyphen-insensitive key for a store URL (two URLs with the same key = same store)."""
    return _url_norm(url)


def dedupe_urls(urls: Iterable[str], alias: dict[str, str]) -> list[str]:
    """Map URLs to canonical and drop duplicates, preserving first-seen order."""
    out, seen = [], set()
    for u in urls:
        c = canonical_url(u, alias)
        k = store_url_key(c)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out
