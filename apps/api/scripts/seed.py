"""Seed the campus locations from db/seed/locations.csv (idempotent: safe to run twice).

Run from apps/api:   python scripts/seed.py

db/schema.sql already inserts the same 14 locations; run this after editing locations.csv.
Lost/found test reports are NOT inserted into the database: every report needs a real
Supabase Auth user (public.users.id references auth.users). Use the offline evaluation
(scripts/evaluate_matching.py) for the 40-report test dataset instead.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402

CSV = Path(__file__).resolve().parents[3] / "db" / "seed" / "locations.csv"


def main() -> None:
    if not db.is_configured():
        sys.exit("DATABASE_URL is empty in apps/api/.env.")
    with CSV.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    with db.transaction() as conn:
        for r in rows:
            db.execute("insert into public.locations (name, zone) values (%(n)s, %(z)s) "
                       "on conflict (name) do update set zone = excluded.zone",
                       {"n": r["name"].strip(), "z": r["zone"].strip()}, conn=conn)
    total = db.fetch_one("select count(*) as n from public.locations")["n"]
    print(f"Upserted {len(rows)} locations; {total} in the table.")
    db.close_pool()


if __name__ == "__main__":
    main()
