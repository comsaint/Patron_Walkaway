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


def _validate_yyyymm(yyyymm: str) -> str:
    """Validate six-digit ``YYYYMM`` or raise."""
    ym = str(yyyymm).strip()
    if len(ym) != 6 or not ym.isdigit():
        raise ValueError(f"month must be six YYYYMM digits, got {yyyymm!r}")
    return ym


def shift_calendar_month(yyyymm: str, *, delta_months: int) -> str:
    """Shift ``YYYYMM`` by *delta_months* on the calendar axis."""
    ym = _validate_yyyymm(yyyymm)
    y = int(ym[:4])
    m = int(ym[4:6])
    m += int(delta_months)
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}{m:02d}"


def label_invalid_months(dirty_months: set[str]) -> set[str]:
    """Expand dirty source months to label safety window (prev + dirty + next)."""
    out: set[str] = set()
    for raw in dirty_months:
        ym = _validate_yyyymm(raw)
        out.add(shift_calendar_month(ym, delta_months=-1))
        out.add(ym)
        out.add(shift_calendar_month(ym, delta_months=1))
    return out


def short_pit_invalid_months(
    dirty_months: set[str],
    *,
    neighbor_backfill: int = 1,
) -> set[str]:
    """Expand dirty months for short-term PIT (dirty + prior neighbor months)."""
    seeds = {_validate_yyyymm(m) for m in dirty_months}
    return backfill_neighbor_months(seeds, backfill_count=int(neighbor_backfill))


def mid_term_invalid_months(
    dirty_months: set[str],
    *,
    lookback_months: int = 32,
) -> set[str]:
    """Expand dirty months for mid-term lookback overlap (MVP month grain)."""
    lb = max(0, int(lookback_months))
    out: set[str] = set()
    for raw in dirty_months:
        ym = _validate_yyyymm(raw)
        cur = ym
        out.add(cur)
        for _ in range(lb):
            cur = shift_calendar_month(cur, delta_months=-1)
            out.add(cur)
    return out
