#!/usr/bin/env python3
"""The vendors.csv contract — the seam between cloud-specific and shared stages.

Any cloud that emits a valid vendors.csv inherits Stages 3-5 unchanged.
Documented in ../ARCHITECTURE.md — do not change the columns without updating
it there.

Validation happens on WRITE (Azure's choice) rather than only on read, so a
bad file never reaches a downstream stage that would fail halfway through.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

VENDOR_COLUMNS = [
    "vendor_key",
    "vendor_name",
    "website_domain",
    "product_fit",
    "listing_count",
    "product_types",
    "best_product_id",
    "best_listing_title",
    "best_listing_url",
    "commercial_signals",
    "b2b_signals",
    "negative_signals",
]
REQUIRED_VENDOR_COLUMNS = {"vendor_key", "vendor_name", "website_domain"}


def write_vendors_csv(path: Path, rows: list[dict[str, Any]]) -> int:
    """Emit the shared contract, or fail loudly.

    Checks every row, not just the first — a missing column in row 4000 is
    just as fatal as one in row 1.
    """
    if not rows:
        raise ValueError("No vendors to export; run qualify first")

    keys = [str(row.get("vendor_key") or "").strip() for row in rows]
    if not all(keys):
        raise ValueError("Every vendor row needs a vendor_key")
    if len(keys) != len(set(keys)):
        duplicates = sorted({k for k in keys if keys.count(k) > 1})[:5]
        raise ValueError(
            f"vendors.csv would contain duplicate vendor_key values: {duplicates}"
        )
    for index, row in enumerate(rows):
        missing = sorted(REQUIRED_VENDOR_COLUMNS - set(row))
        if missing:
            raise ValueError(
                f"vendors.csv row {index} is missing required columns: {', '.join(missing)}"
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=VENDOR_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def read_vendors_csv(path: Path) -> list[dict[str, str]]:
    """Read a vendors.csv, enforcing the same contract on the way in."""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [{k: (v or "") for k, v in row.items()} for row in reader]
    if not rows:
        raise ValueError(f"{path} contains no vendor rows")
    missing = sorted(REQUIRED_VENDOR_COLUMNS - set(rows[0]))
    if missing:
        raise ValueError(f"{path} is missing required columns: {', '.join(missing)}")
    keys = [r["vendor_key"].strip() for r in rows]
    if not all(keys):
        raise ValueError(f"{path} has rows with a blank vendor_key")
    if len(keys) != len(set(keys)):
        raise ValueError(f"{path} has duplicate vendor_key values")
    return rows
