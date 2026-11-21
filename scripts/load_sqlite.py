"""Load all data/live CSVs (and data/ref) into data/db/vvv.sqlite for local analysis.

Idempotent: tables are rebuilt from the CSVs on every run.
    python scripts/load_sqlite.py [--db data/db/vvv.sqlite]

Tables:
  wait_times(ts_utc, store_url, wait_bucket, status, source_file)
  coupons(ts_utc, store_url, rating, review_count, coupons_json, source_file)
  jobs(ts_utc, total_jobs, page, job_url, title, address, label, source_file)
  jobs_daily_total(date, ts_utc, total_jobs)         -- one row per daily snapshot
  ref_stores, ref_cities, ref_wait_sample, ref_coupon_sample
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "data" / "live"
REF = ROOT / "data" / "ref"

INDEXES = {
    "wait_times": ["store_url", "ts_utc"],
    "coupons": ["store_url", "ts_utc"],
    "jobs": ["ts_utc"],
}


def load_dir(kind: str) -> pd.DataFrame:
    frames = []
    for p in sorted((LIVE / kind).glob("*.csv")):
        df = pd.read_csv(p, dtype=str, keep_default_na=False)
        df["source_file"] = p.name
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "db" / "vvv.sqlite"))
    args = ap.parse_args()
    db = Path(args.db)
    db.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db) as con:
        for kind in ("wait_times", "coupons", "jobs"):
            df = load_dir(kind)
            if df.empty:
                print(f"{kind}: no CSVs")
                continue
            for col in ("rating",):
                if col in df:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            for col in ("review_count", "total_jobs", "page"):
                if col in df:
                    df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
            df = df.drop_duplicates()
            df.to_sql(kind, con, if_exists="replace", index=False)
            for c in INDEXES[kind]:
                con.execute(f"CREATE INDEX IF NOT EXISTS ix_{kind}_{c} ON {kind}({c})")
            print(f"{kind}: {len(df):,} rows from {df['source_file'].nunique()} files")
            if kind == "jobs":
                t = (df.dropna(subset=["total_jobs"]).groupby("ts_utc", as_index=False)["total_jobs"].max())
                t.insert(0, "date", t["ts_utc"].str[:10])
                t.to_sql("jobs_daily_total", con, if_exists="replace", index=False)

        for name in ("stores", "cities", "wait_sample", "coupon_sample"):
            p = REF / f"{name}.csv"
            if p.exists():
                df = pd.read_csv(p, dtype=str, keep_default_na=False)
                df.to_sql(f"ref_{name}", con, if_exists="replace", index=False)
                print(f"ref_{name}: {len(df):,} rows")
    print(f"-> {db}")


if __name__ == "__main__":
    main()
