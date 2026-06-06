"""L1 recompute month selection from source manifest v2 content diff."""

from __future__ import annotations

from typing import Any, Final

from trainer_hightier.utils.partition_inventory import (
    backfill_neighbor_months,
    merge_correction_months,
)

RECOMPUTE_SOURCE_V2: Final[str] = "source_manifest_v2"
SOURCE_MANIFEST_V2_FP_KEY: Final[str] = "source_manifest_v2_fingerprint_sha256_hex"


def attach_l1_source_identity(
    body: dict[str, Any],
    *,
    source_manifest_v2_fingerprint_sha256_hex: str | None,
    partition_inventory_fingerprint_sha256_hex: str | None,
) -> None:
    """Attach L1 cache source identity (prefer content-addressed manifest v2)."""
    if source_manifest_v2_fingerprint_sha256_hex is not None:
        fp = str(source_manifest_v2_fingerprint_sha256_hex).strip()
        if fp:
            body[SOURCE_MANIFEST_V2_FP_KEY] = fp
            return
    if partition_inventory_fingerprint_sha256_hex is not None:
        inv = str(partition_inventory_fingerprint_sha256_hex).strip()
        if inv:
            body["partition_inventory_fingerprint_sha256_hex"] = inv


def union_changed_partition_months(changed_partitions: dict[str, Any]) -> set[str]:
    """Union ``YYYYMM`` months from ``changed_partitions`` bet/session lists."""
    if not isinstance(changed_partitions, dict):
        raise TypeError(
            f"changed_partitions must be dict, got {type(changed_partitions).__name__}",
        )
    out: set[str] = set()
    for table in ("t_bet", "t_session"):
        rows = changed_partitions.get(table)
        if not isinstance(rows, list):
            continue
        for ym in rows:
            s = str(ym).strip()
            if len(s) != 6 or not s.isdigit():
                raise ValueError(
                    f"changed_partitions[{table!r}] entry must be YYYYMM digits, got {ym!r}",
                )
            out.add(s)
    return out


def compute_l1_recompute_months(
    *,
    changed_partitions: dict[str, Any],
    correction_months: tuple[str, ...],
    backfill_month_count: int,
    available_months: set[str],
) -> list[str]:
    """Merge source content-diff months with correction/backfill; keep only on-disk months."""
    if not isinstance(available_months, set):
        raise TypeError(
            f"available_months must be set, got {type(available_months).__name__}",
        )
    changed = union_changed_partition_months(changed_partitions)
    pool = merge_correction_months(
        backfill_neighbor_months(changed, backfill_count=int(backfill_month_count)),
        correction_months=correction_months,
    )
    pool &= set(available_months)
    return sorted(pool)
