"""Fail-closed guards for GitHub #16 training semantics (split / labels / metrics).

Gates are **pure** (no I/O).  ``trainer.run_pipeline`` may call
``evaluate_issue16_gate_bundle`` after Step 7 metadata exists.

Strict mode (raises ``RuntimeError``): set environment variable
``TRAINER_ISSUE16_STRICT_GATES=1`` (or ``true`` / ``yes``).  Default is advisory
only so existing chunk pipelines keep working until L2 migration completes.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional, Tuple

from trainer.training.l2_trainer_contracts import (
    KEY_TEST_FULL_UNSAMPLED,
    KEY_VALID_FULL_UNSAMPLED,
    SNAPSHOT_ID_ALIASES,
    SPLIT_SAMPLING_CONTRACT_VERSION,
    TRAIN_END_SOURCE_CHUNK_SPLIT,
    TRAIN_END_SOURCE_L2_MANIFEST,
)

# Minimum keys to compare training lineage vs label sidecar (subset of full label_asset schema).
_LABEL_ASSET_LINEAGE_KEYS_FOR_GUARD: Tuple[str, ...] = (
    "source_snapshot_id",
    "label_definition_version",
    "coverage_end",
)


def _truthy_env(name: str) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def strict_issue16_gates_enabled() -> bool:
    """Return True when fail-closed #16 gates should abort the pipeline."""
    return _truthy_env("TRAINER_ISSUE16_STRICT_GATES")


def valid_test_sampling_guard(*, effective_neg_sample_frac: float) -> Tuple[bool, str]:
    """Check that valid/test rows were not subject to per-chunk negative downsampling.

    Today negatives are downsampled inside ``process_chunk`` **before** row-level
    split, so any ``effective_neg_sample_frac < 1.0`` violates #16 full valid/test
    label distribution semantics.
    """
    if effective_neg_sample_frac >= 1.0 - 1e-12:
        return True, "effective_neg_sample_frac>=1.0 (no per-chunk neg downsample)"
    return (
        False,
        f"effective_neg_sample_frac={effective_neg_sample_frac:.6f} "
        "< 1.0 implies valid/test may see downsampled negatives (forbidden under #16)",
    )


def split_contract_guard(
    *,
    chunk_train_end_naive: Any,
    row_level_train_end_max: Any,
    train_end_source: str,
) -> Tuple[bool, str]:
    """Require chunk-derived train_end to match row-level max train payout time.

    When both are parseable and unequal, the pipeline does not have a single
    train-end boundary (leakage / drift risk per #16).

    For the legacy chunk path (``TRAIN_END_SOURCE_CHUNK_SPLIT``), this check is
    **skipped** because row-level boundaries can diverge from chunk ``window_end``
    (trainer R700/R701).  The L2 manifest path must carry a single canonical
    ``train_end``; when *train_end_source* is ``TRAIN_END_SOURCE_L2_MANIFEST``,
    mismatches fail this gate.
    """
    import pandas as pd

    if train_end_source == TRAIN_END_SOURCE_CHUNK_SPLIT:
        return True, "chunk legacy path: train_end vs row_max alignment not enforced (R700/R701)"
    if train_end_source != TRAIN_END_SOURCE_L2_MANIFEST:
        return True, f"train_end_source={train_end_source!r}: no split_contract rule"

    if chunk_train_end_naive is None or row_level_train_end_max is None:
        return True, "skip: missing chunk or row-level train_end for comparison"
    try:
        a = pd.Timestamp(chunk_train_end_naive)
        b = pd.Timestamp(row_level_train_end_max)
    except Exception as exc:  # noqa: BLE001 — defensive parse
        return True, f"skip: non-parseable train_end values ({exc!r})"
    if a.tzinfo is not None:
        a = pd.Timestamp(a).tz_convert("Asia/Hong_Kong")
    a = pd.Timestamp(a).replace(tzinfo=None)
    if b.tzinfo is not None:
        b = pd.Timestamp(b).tz_convert("Asia/Hong_Kong")
    b = pd.Timestamp(b).replace(tzinfo=None)
    if a == b:
        return True, "chunk_train_end matches row_level_train_end_max"
    return False, f"train_end mismatch: chunk={a!s} row_max={b!s}"


def metric_semantics_guard(*, effective_neg_sample_frac: float) -> Tuple[bool, str]:
    """Ensure PR-AUC / ROC / logloss on valid/test reflect raw row populations.

    For the legacy pipeline this collapses to the same check as
    ``valid_test_sampling_guard`` (neg downsample poisons all splits).
    """
    return valid_test_sampling_guard(effective_neg_sample_frac=effective_neg_sample_frac)


def label_asset_freshness_guard(
    *,
    training_source_snapshot_id: Optional[str],
    label_asset_meta: Optional[Mapping[str, Any]],
) -> Tuple[bool, str]:
    """Verify label_asset lineage keys match the training snapshot when provided.

    If *label_asset_meta* is None, the gate passes (labels still computed inline).
    """
    if not label_asset_meta:
        return True, "no label_asset_meta (inline labels path)"
    missing = [c for c in _LABEL_ASSET_LINEAGE_KEYS_FOR_GUARD if c not in label_asset_meta]
    if missing:
        return False, f"label_asset_meta missing keys: {missing}"
    ids = [str(label_asset_meta.get(k) or "").strip() for k in SNAPSHOT_ID_ALIASES if k in label_asset_meta]
    ids = [x for x in ids if x]
    if len(set(ids)) > 1:
        return False, f"label_asset_meta has conflicting snapshot ids: {ids!r}"
    if training_source_snapshot_id and ids:
        if training_source_snapshot_id.strip() not in ids:
            return (
                False,
                f"source_snapshot_id mismatch: training={training_source_snapshot_id!r} "
                f"label_asset={ids!r}",
            )
    return True, "label_asset_meta present and snapshot consistent"


def l2_manifest_train_end_guard(*, train_end_source: str, l2_snapshot_id: Optional[str]) -> Tuple[bool, str]:
    """When claiming L2 manifest train_end, require a snapshot id for audit."""
    if train_end_source != TRAIN_END_SOURCE_L2_MANIFEST:
        return True, "non-L2 train_end source"
    if l2_snapshot_id and str(l2_snapshot_id).strip():
        return True, "L2 snapshot id present"
    return False, "train_end_source=l2_manifest but l2_snapshot_id empty"


def evaluate_issue16_gate_bundle(
    *,
    effective_neg_sample_frac: float,
    chunk_train_end_naive: Any,
    row_level_train_end_max: Any,
    train_end_source: str = "chunk_level_train_end",
    l2_snapshot_id: Optional[str] = None,
    label_asset_meta: Optional[Mapping[str, Any]] = None,
    training_source_snapshot_id: Optional[str] = None,
    split_flags: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Run all #16 gates and return a JSON-serialisable report."""
    gates: Dict[str, Dict[str, Any]] = {}
    ok_v, msg_v = valid_test_sampling_guard(effective_neg_sample_frac=effective_neg_sample_frac)
    gates["valid_test_sampling_guard"] = {"ok": ok_v, "detail": msg_v}
    ok_s, msg_s = split_contract_guard(
        chunk_train_end_naive=chunk_train_end_naive,
        row_level_train_end_max=row_level_train_end_max,
        train_end_source=train_end_source,
    )
    gates["split_contract_guard"] = {"ok": ok_s, "detail": msg_s}
    ok_m, msg_m = metric_semantics_guard(effective_neg_sample_frac=effective_neg_sample_frac)
    gates["metric_semantics_guard"] = {"ok": ok_m, "detail": msg_m}
    ok_l, msg_l = label_asset_freshness_guard(
        training_source_snapshot_id=training_source_snapshot_id,
        label_asset_meta=label_asset_meta,
    )
    gates["label_asset_freshness_guard"] = {"ok": ok_l, "detail": msg_l}
    ok_l2, msg_l2 = l2_manifest_train_end_guard(
        train_end_source=train_end_source,
        l2_snapshot_id=l2_snapshot_id,
    )
    gates["l2_manifest_train_end_guard"] = {"ok": ok_l2, "detail": msg_l2}

    if split_flags:
        vf = bool(split_flags.get(KEY_VALID_FULL_UNSAMPLED))
        tf = bool(split_flags.get(KEY_TEST_FULL_UNSAMPLED))
        gates["explicit_split_flags"] = {
            "ok": vf and tf,
            "detail": f"{KEY_VALID_FULL_UNSAMPLED}={vf} {KEY_TEST_FULL_UNSAMPLED}={tf}",
        }
    all_ok = all(v.get("ok") for v in gates.values())
    return {
        "issue16_gate_contract_version": "2026-05-07",
        "split_sampling_contract_version": SPLIT_SAMPLING_CONTRACT_VERSION,
        "strict_gates_enabled": strict_issue16_gates_enabled(),
        "all_ok": all_ok,
        "gates": gates,
    }


def raise_if_strict_issue16_gates_failed(report: Mapping[str, Any]) -> None:
    """Raise ``RuntimeError`` when strict mode is on and any gate failed."""
    if not strict_issue16_gates_enabled():
        return
    gates = report.get("gates") or {}
    failed: List[str] = []
    for name, body in gates.items():
        if isinstance(body, dict) and not body.get("ok", False):
            failed.append(f"{name}: {body.get('detail', '')}")
    if failed:
        raise RuntimeError(
            "TRAINER_ISSUE16_STRICT_GATES: pipeline blocked — " + " | ".join(failed)
        )
