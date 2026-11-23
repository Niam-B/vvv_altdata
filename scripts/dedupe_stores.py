"""De-duplicate data/ref/stores.csv in place (see collectors.common.dedupe_stores) and record
removed URLs in data/ref/store_url_aliases.csv (alias_url -> canonical store_url).

Idempotent. Existing aliases are kept, so old URLs in past data/live CSVs and the fixed
samples can always be mapped to the canonical store.
    python scripts/dedupe_stores.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.common import (  # noqa: E402
    ALIAS_FIELDS, ALIASES_CSV, REF, dedupe_stores, load_aliases, read_csv, write_csv,
)


def main() -> int:
    path = REF / "stores.csv"
    rows = read_csv(path)
    fields = list(rows[0].keys()) if rows else []
    kept, alias = dedupe_stores(rows)
    aliases = {**load_aliases(), **{a: c for a, c in alias.items() if a != c}}
    # resolve chains (a -> b -> c) so every alias points at a URL still in stores.csv
    live = {r["store_url"] for r in kept}
    for a in list(aliases):
        seen = set()
        while aliases[a] not in live and aliases[a] in aliases and aliases[a] not in seen:
            seen.add(aliases[a])
            aliases[a] = aliases[aliases[a]]
    write_csv(path, fields, kept)
    write_csv(REF / ALIASES_CSV, ALIAS_FIELDS,
              [{"alias_url": a, "store_url": c} for a, c in sorted(aliases.items())])
    print(f"stores.csv: {len(rows)} -> {len(kept)} rows ({len(rows) - len(kept)} duplicates removed); "
          f"{len(aliases)} aliases in data/ref/{ALIASES_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
