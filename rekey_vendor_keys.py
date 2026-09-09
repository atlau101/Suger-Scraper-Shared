#!/usr/bin/env python3
"""Re-key any partner-status database after the name-normalisation rules change.

Works for both clouds — pass --db. Lives in shared/ because both Azure and GCP
need it and the logic mentions neither.

WHY THIS EXISTS

`vendor_key` is `sha256(canonical_company_name(name))[:20]`. When the shared
legal-suffix list changed (2026-09-07, reconciling GCP's two drifted copies),
the key changed for ~2% of companies. Stage 1 handles that natively — its
`qualify` deletes and rebuilds every downstream row, and the code says so.

Stage 2 has no such path. Its only option would be re-running every lookup,
and GCP's Stage 2 drives a real browser for each confirmed vendor, so a full
re-run costs hours. But the lookup RESULT depends only on `vendor_name` and
`website_domain`, never on `vendor_key` — the key is just the address the
result is filed under. So re-filing is exact and takes seconds.

SAFETY

This rewrites primary keys, so it refuses to write unless the remap is
lossless, and makes you opt in when it is not:

  * no row may end up with an empty key — hard failure, always
  * a within-run collision means two vendors now share one key, so one lookup
    result must be discarded. That is a real merge and needs a decision, so it
    fails unless you pass --merge. With --merge, the row with the best lookup
    status survives (confirmed > match_review > not_found > lookup_error) and
    the others are deleted.

Collisions are judged PER RUN, because the primary key is
(run_id, vendor_key). The same company appearing in two runs under two
different old keys is the same rename applied twice, not a merge.

Two-phase update: every key is first prefixed with a marker, then written to
its final value. A single-phase UPDATE can transiently violate the composite
primary key if one row's new key equals another row's current key.

Pass --alias-out to record old->new pairs. Anything filed by vendor_key
elsewhere (research verdicts, for instance) can then still be joined after the
re-key instead of silently orphaning.

Back up the database before running. --apply is required to write.

Usage:
    python3 rekey_vendor_keys.py --db X.sqlite
    python3 rekey_vendor_keys.py --db X.sqlite --merge --apply \
        --alias-out azure_joined_output/key_aliases.csv
"""

from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from normalize import vendor_key  # noqa: E402

MARKER = "__rekey__"


def keyed_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Every table in THIS database that has a vendor_key column.

    Discovered rather than hardcoded: the clouds' schemas differ (GCP keeps
    `profiles` and `match_reviews`; Azure stores the partner record inline on
    `lookups`), and a hardcoded list either crashes on the wrong cloud or,
    worse, silently skips a table and leaves half the database on old keys.
    """
    found = []
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({row['name']})")]
        if "vendor_key" in cols:
            found.append(row["name"])
    if "vendors" not in found:
        raise RuntimeError(f"{'/'.join(found) or 'database'} has no vendors table to remap from")
    # vendors carries vendor_name and is the source of the remap; rename it last
    # so a crash mid-run leaves the mapping recoverable.
    return tuple(sorted(found, key=lambda t: t == "vendors"))


# When a merge collapses two rows into one, this decides which survives.
# Better evidence wins; a transport failure never beats a real answer.
STATUS_RANK = {
    "confirmed": 0,
    "match_review": 1,
    "not_found_in_searched_source": 2,
    "lookup_error": 3,
    "pending": 4,
}


def build_remap(conn: sqlite3.Connection) -> dict[str, str]:
    """old_key -> new_key, for every key in the database.

    A single old key always maps to exactly one new key, because both are pure
    functions of the vendor name.
    """
    remap: dict[str, str] = {}
    for row in conn.execute("SELECT DISTINCT vendor_key, vendor_name FROM vendors"):
        new = vendor_key(row["vendor_name"])
        if not new:
            raise RuntimeError(f"{row['vendor_name']!r} would get an empty key; refusing")
        remap[row["vendor_key"]] = new
    return remap


def find_merges(conn: sqlite3.Connection, remap: dict[str, str]) -> dict[tuple[str, str], list[str]]:
    """Return {(run_id, new_key): [old keys]} for real collisions only.

    Collisions matter **per run**, because the primary key is
    (run_id, vendor_key). The same company appearing in two runs under two
    different old keys is not a collision — it is the same rename applied
    twice, and treating it as a merge would refuse a perfectly safe re-key.
    That distinction is the whole reason this function is scoped by run.
    """
    landing: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    for row in conn.execute("SELECT run_id, vendor_key FROM vendors"):
        landing[(row["run_id"], remap[row["vendor_key"]])].add(row["vendor_key"])
    return {k: sorted(v) for k, v in landing.items() if len(v) > 1}


def resolve_merge(conn: sqlite3.Connection, run_id: str, old_keys: list[str]) -> str:
    """Pick the old key whose lookup carries the best evidence."""
    best, best_rank = old_keys[0], 99
    for old in old_keys:
        row = conn.execute(
            "SELECT status, completed_at FROM lookups WHERE run_id=? AND vendor_key=?",
            (run_id, old),
        ).fetchone()
        rank = STATUS_RANK.get(row["status"] if row else "pending", 99)
        if rank < best_rank:
            best, best_rank = old, rank
    return best


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True,
                        help="partner-status sqlite to re-key")
    parser.add_argument("--alias-out", type=Path,
                        help="append old->new pairs here so research verdicts "
                             "filed under superseded keys keep joining")
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--merge", action="store_true",
                        help="allow real within-run merges, keeping the row with "
                             "the best lookup status and deleting the others")
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"error: {args.db} does not exist", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        tables = keyed_tables(conn)
        remap = build_remap(conn)
        changing = {old: new for old, new in remap.items() if old != new}
        merges = find_merges(conn, remap)

        if merges and not args.merge:
            sample = []
            for (run, new), olds in list(merges.items())[:4]:
                names = [
                    r["vendor_name"] for r in conn.execute(
                        "SELECT vendor_name FROM vendors WHERE run_id=? AND vendor_key IN "
                        f"({','.join('?' * len(olds))})", [run, *olds])
                ]
                sample.append(sorted(set(names)))
            raise RuntimeError(
                f"{len(merges)} within-run collisions: two vendors now share a key, "
                f"so one lookup result must be discarded. Review these, then re-run "
                f"with --merge to keep the best-status row: {sample}"
            )

        counts = {}
        for table in tables:
            placeholders = ",".join("?" * len(changing)) if changing else "NULL"
            counts[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE vendor_key IN ({placeholders})",
                list(changing),
            ).fetchone()[0] if changing else 0

        report = {
            "database": str(args.db),
            "distinct_keys": len(remap),
            "keys_changing": len(changing),
            "tables": list(tables),
            "rows_affected": counts,
            "applied": False,
        }

        if args.alias_out and changing:
            import csv as _csv
            from datetime import UTC, datetime as _dt
            args.alias_out.parent.mkdir(parents=True, exist_ok=True)
            names = {r["vendor_key"]: r["vendor_name"]
                     for r in conn.execute("SELECT vendor_key, vendor_name FROM vendors")}
            exists = args.alias_out.exists()
            with args.alias_out.open("a", encoding="utf-8-sig", newline="") as h:
                w = _csv.DictWriter(h, fieldnames=["old_key", "new_key",
                                                   "vendor_name", "recorded_at"])
                if not exists:
                    w.writeheader()
                stamp = _dt.now(UTC).replace(microsecond=0).isoformat()
                for old, new in changing.items():
                    w.writerow({"old_key": old, "new_key": new,
                                "vendor_name": names.get(old, ""),
                                "recorded_at": stamp})
            report["aliases_appended"] = len(changing)

        report["within_run_merges"] = len(merges)

        if args.apply and merges:
            # Delete the losing rows BEFORE renaming, so the rename cannot hit
            # a duplicate primary key.
            for (run, _new), olds in merges.items():
                keep = resolve_merge(conn, run, olds)
                for old in olds:
                    if old == keep:
                        continue
                    for table in tables:
                        conn.execute(
                            f"DELETE FROM {table} WHERE run_id=? AND vendor_key=?",
                            (run, old))
            conn.commit()

        if args.apply and changing:
            conn.execute("BEGIN")
            for table in tables:
                for old, new in changing.items():
                    conn.execute(
                        f"UPDATE {table} SET vendor_key=? WHERE vendor_key=?",
                        (MARKER + new, old),
                    )
            for table in tables:
                conn.execute(
                    f"UPDATE {table} SET vendor_key=REPLACE(vendor_key, ?, '') "
                    f"WHERE vendor_key LIKE ?",
                    (MARKER, MARKER + "%"),
                )
            conn.commit()
            report["applied"] = True

            leftover = sum(
                conn.execute(
                    f"SELECT COUNT(*) FROM {t} WHERE vendor_key LIKE ?", (MARKER + "%",)
                ).fetchone()[0]
                for t in tables
            )
            stale = conn.execute(
                "SELECT COUNT(*) FROM vendors WHERE vendor_key != ''"
            ).fetchone()[0]
            bad = [
                r["vendor_name"] for r in conn.execute(
                    "SELECT vendor_key, vendor_name FROM vendors")
                if r["vendor_key"] != vendor_key(r["vendor_name"])
            ]
            report["verify"] = {
                "marker_rows_left": leftover,
                "vendor_rows": stale,
                "keys_still_wrong": len(bad),
            }

        print(json.dumps(report, indent=2))
        if not args.apply:
            print("\nDry run. Re-run with --apply to write.", file=sys.stderr)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
