"""Paths, CSV helpers and deterministic stratified sampling shared by collectors."""
from __future__ import annotations

import csv
import logging
import random
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
