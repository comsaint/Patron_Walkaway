"""Load and validate frozen ``feature_candidate_registry`` YAML (serving-safe, in wheel).

Used by deploy preflight, packaging gates, and training via re-export from
``trainer_hightier.feature_experiment.candidate_registry_loader``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pandas as pd
import yaml

_REGISTRY_STATUSES: Final[frozenset[str]] = frozenset({"active", "disabled", "experimental"})
_REGISTRY_SLOTS: Final[frozenset[str]] = frozenset({"baseline", "candidate", "ablation"})
_REGISTRY_HORIZONS: Final[frozenset[str]] = frozenset({"none", "short_term", "mid_term", "long_term"})
_RUNTIME_INPUT_SUPPLIERS: Final[frozenset[str]] = frozenset(
    {
        "feast_online_mid",
        "feast_online_slow",
        "short_term_pit_builder",
        "clickhouse_raw",
        "feast_trial_1h",
        "txn_lite_builder",
    }
)
_RUNTIME_SUPPLIERS: Final[frozenset[str]] = _RUNTIME_INPUT_SUPPLIERS | frozenset({"composite"})

# Boundaries (Packaging / Feature Candidate Registry plans): compare using pandas Timedeltas.
_BOUNDARY_SHORT_MAX: Final[pd.Timedelta] = pd.Timedelta("PT24H")
_BOUNDARY_MID_MAX: Final[pd.Timedelta] = pd.Timedelta("P30D")


@dataclass(frozen=True)
class FeatureRegistryEntryRow:
    """One row from the candidate registry YAML."""

    feature_id: str
    group_id: str
    source: str
    status: str
    enabled_for: tuple[str, ...]
    drop_reason_code: str | None
    semantic_owner: str | None
    first_seen_experiment: str | None
    last_updated_experiment: str | None
    note: str | None
    time_horizon: str
    max_lookback: str | None
    cadence: str | None = None
    anchor_rule: str | None = None
    grain: str | None = None
    allowed_training_supplier: str | None = None
    runtime_supplier: str | None = None
    runtime_inputs: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass(frozen=True)
class CandidateRegistrySnapshot:
    """Validated registry snapshot for feature supply and selection."""

    registry_version: str
    updated_at: str | None
    path: Path
    rows: tuple[FeatureRegistryEntryRow, ...]
    model_feature_columns: tuple[str, ...]
    experimental_numeric_columns: tuple[str, ...]
    full_candidate_feature_columns: tuple[str, ...]
    feature_group_tags: dict[str, tuple[str, ...]]
    ablation_experimental_group_ids: tuple[str, ...]


def _duration_seconds_from_iso8601(s: str) -> float:
    """Parse ISO-8601 duration to seconds (``pandas.Timedelta``)."""

    raw = str(s).strip()
    try:
        td = pd.Timedelta(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"max_lookback must be a valid ISO-8601 duration, got {s!r}") from exc
    if pd.isna(td):
        raise ValueError(f"max_lookback is not a valid ISO-8601 duration, got {s!r}")
    sec = float(td.total_seconds())
    if sec <= 0:
        raise ValueError(f"max_lookback must be a positive duration, got {s!r}")
    return sec


def _parse_runtime_inputs(idx: int, fid: str, raw: Any) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Parse ``runtime_inputs`` mapping supplier -> feature id list."""

    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise TypeError(f"features[{idx}].runtime_inputs must be a mapping for {fid!r}, got {type(raw)}")
    out: list[tuple[str, tuple[str, ...]]] = []
    for key, val in raw.items():
        supplier = str(key).strip()
        if supplier not in _RUNTIME_INPUT_SUPPLIERS:
            allowed = ", ".join(sorted(_RUNTIME_INPUT_SUPPLIERS))
            raise ValueError(
                f"features[{idx}] {fid!r}: runtime_inputs key {supplier!r} invalid; "
                f"expected one of [{allowed}]",
            )
        if not isinstance(val, list) or not val:
            raise ValueError(
                f"features[{idx}] {fid!r}: runtime_inputs[{supplier!r}] must be a non-empty list",
            )
        deps = tuple(str(x).strip() for x in val if str(x).strip())
        if len(deps) != len(val):
            raise ValueError(
                f"features[{idx}] {fid!r}: runtime_inputs[{supplier!r}] contains empty feature ids",
            )
        out.append((supplier, deps))
    return tuple(out)


def horizon_from_max_lookback_iso8601(max_lookback: str) -> str:
    """Map ``max_lookback`` to ``time_horizon`` (short / mid / long) per Packaging plan."""

    td = pd.Timedelta(str(max_lookback).strip())
    if td < _BOUNDARY_SHORT_MAX:
        return "short_term"
    if td <= _BOUNDARY_MID_MAX:
        return "mid_term"
    return "long_term"


def _row_from_raw(idx: int, raw: dict[str, Any]) -> FeatureRegistryEntryRow:
    if not isinstance(raw, dict):
        raise TypeError(f"features[{idx}] must be a mapping, got {type(raw)}")
    fid = raw.get("feature_id")
    if not isinstance(fid, str) or not fid.strip():
        raise ValueError(f"features[{idx}].feature_id must be a non-empty string")
    gid = raw.get("group_id")
    if not isinstance(gid, str) or not gid.strip():
        raise ValueError(f"features[{idx}].group_id must be a non-empty string for {fid!r}")
    src = raw.get("source")
    if not isinstance(src, str) or not src.strip():
        raise ValueError(f"features[{idx}].source must be a non-empty string for {fid!r}")
    st = raw.get("status")
    if not isinstance(st, str) or st not in _REGISTRY_STATUSES:
        raise ValueError(
            f"features[{idx}].status must be one of {sorted(_REGISTRY_STATUSES)} for {fid!r}, got {st!r}",
        )
    ef = raw.get("enabled_for")
    if not isinstance(ef, list) or not ef:
        raise ValueError(f"features[{idx}].enabled_for must be a non-empty list for {fid!r}")
    slots = tuple(str(x) for x in ef)
    for s in slots:
        if s not in _REGISTRY_SLOTS:
            raise ValueError(f"features[{idx}].enabled_for has invalid slot {s!r} for {fid!r}")
    drc = raw.get("drop_reason_code")
    if st == "disabled":
        if drc is None or (isinstance(drc, str) and not drc.strip()):
            raise ValueError(f"features[{idx}].drop_reason_code is required when status=disabled ({fid!r})")
    elif drc is not None and not isinstance(drc, str):
        raise TypeError(f"features[{idx}].drop_reason_code must be str or null for {fid!r}")
    so = raw.get("semantic_owner")
    if so is not None and not isinstance(so, str):
        raise TypeError(f"features[{idx}].semantic_owner must be str or null for {fid!r}")
    fse = raw.get("first_seen_experiment")
    if fse is not None and not isinstance(fse, str):
        raise TypeError(f"features[{idx}].first_seen_experiment must be str or null for {fid!r}")
    lue = raw.get("last_updated_experiment")
    if lue is not None and not isinstance(lue, str):
        raise TypeError(f"features[{idx}].last_updated_experiment must be str or null for {fid!r}")
    note = raw.get("note")
    if note is not None and not isinstance(note, str):
        raise TypeError(f"features[{idx}].note must be str or null for {fid!r}")
    src_stripped = src.strip()
    th_raw = raw.get("time_horizon")
    ml_raw = raw.get("max_lookback")

    if src_stripped == "baseline_model":
        if th_raw is None:
            time_horizon = "none"
        elif isinstance(th_raw, str) and th_raw.strip():
            time_horizon = th_raw.strip()
        else:
            raise TypeError(f"features[{idx}].time_horizon must be str or null for {fid!r}")
        if time_horizon not in _REGISTRY_HORIZONS:
            raise ValueError(
                f"features[{idx}].time_horizon must be one of {sorted(_REGISTRY_HORIZONS)} for {fid!r}, "
                f"got {time_horizon!r}",
            )
        if time_horizon != "none":
            raise ValueError(
                f"features[{idx}] {fid!r}: baseline_model rows must use time_horizon=none, got {time_horizon!r}",
            )
        if ml_raw is not None:
            if not isinstance(ml_raw, str):
                raise TypeError(f"features[{idx}].max_lookback must be str or null for {fid!r}")
            if ml_raw.strip():
                raise ValueError(
                    f"features[{idx}] {fid!r}: baseline_model rows must omit max_lookback or leave null",
                )
        max_lookback_out: str | None = None
    else:
        if not isinstance(th_raw, str) or not th_raw.strip():
            raise ValueError(
                f"features[{idx}].time_horizon is required (non-empty string) for source={src_stripped!r} ({fid!r})",
            )
        time_horizon = th_raw.strip()
        if time_horizon not in _REGISTRY_HORIZONS:
            raise ValueError(
                f"features[{idx}].time_horizon must be one of {sorted(_REGISTRY_HORIZONS)} for {fid!r}, "
                f"got {time_horizon!r}",
            )
        if time_horizon == "none":
            raise ValueError(f"features[{idx}] {fid!r}: time_horizon=none is only valid for baseline_model")
        if not isinstance(ml_raw, str) or not ml_raw.strip():
            raise ValueError(
                f"features[{idx}].max_lookback is required (ISO-8601 duration) for window feature {fid!r}",
            )
        max_lookback_out = ml_raw.strip()
        _duration_seconds_from_iso8601(max_lookback_out)
        expected_h = horizon_from_max_lookback_iso8601(max_lookback_out)
        supplier_override = str(raw.get("allowed_training_supplier") or "").strip()
        cadence_override = str(raw.get("cadence") or "").strip()
        allow_event_level_short_pit = (
            time_horizon == "short_term"
            and supplier_override == "short_term_pit_builder"
            and cadence_override == "event_level"
            and expected_h == "mid_term"
        )
        if time_horizon != expected_h and not allow_event_level_short_pit:
            raise ValueError(
                f"features[{idx}] {fid!r}: time_horizon={time_horizon!r} inconsistent with max_lookback="
                f"{max_lookback_out!r} (expected time_horizon={expected_h!r})",
            )
    cadence_raw = raw.get("cadence")
    anchor_raw = raw.get("anchor_rule")
    grain_raw = raw.get("grain")
    supplier_raw = raw.get("allowed_training_supplier")
    runtime_supplier_raw = raw.get("runtime_supplier")
    runtime_inputs_raw = raw.get("runtime_inputs")
    for key, val in (
        ("cadence", cadence_raw),
        ("anchor_rule", anchor_raw),
        ("grain", grain_raw),
        ("allowed_training_supplier", supplier_raw),
        ("runtime_supplier", runtime_supplier_raw),
    ):
        if val is not None and (not isinstance(val, str) or not str(val).strip()):
            raise TypeError(f"features[{idx}].{key} must be a non-empty string or null for {fid!r}")
    runtime_supplier_out: str | None = None
    if runtime_supplier_raw is not None:
        runtime_supplier_out = str(runtime_supplier_raw).strip()
        if runtime_supplier_out not in _RUNTIME_SUPPLIERS:
            allowed = ", ".join(sorted(_RUNTIME_SUPPLIERS))
            raise ValueError(
                f"features[{idx}] {fid!r}: runtime_supplier={runtime_supplier_out!r} invalid; "
                f"expected one of [{allowed}]",
            )
    runtime_inputs_out = _parse_runtime_inputs(idx, fid.strip(), runtime_inputs_raw)
    if runtime_supplier_out == "composite" and not runtime_inputs_out:
        raise ValueError(
            f"features[{idx}] {fid!r}: runtime_supplier=composite requires non-empty runtime_inputs",
        )
    return FeatureRegistryEntryRow(
        feature_id=fid.strip(),
        group_id=gid.strip(),
        source=src_stripped,
        status=st,
        enabled_for=slots,
        drop_reason_code=str(drc).strip() if isinstance(drc, str) and drc.strip() else None,
        semantic_owner=str(so).strip() if isinstance(so, str) and so.strip() else None,
        first_seen_experiment=str(fse).strip() if isinstance(fse, str) and fse.strip() else None,
        last_updated_experiment=str(lue).strip() if isinstance(lue, str) and lue.strip() else None,
        note=str(note).strip() if isinstance(note, str) and note.strip() else None,
        time_horizon=time_horizon,
        max_lookback=max_lookback_out,
        cadence=str(cadence_raw).strip() if isinstance(cadence_raw, str) and cadence_raw.strip() else None,
        anchor_rule=str(anchor_raw).strip() if isinstance(anchor_raw, str) and anchor_raw.strip() else None,
        grain=str(grain_raw).strip() if isinstance(grain_raw, str) and grain_raw.strip() else None,
        allowed_training_supplier=(
            str(supplier_raw).strip() if isinstance(supplier_raw, str) and supplier_raw.strip() else None
        ),
        runtime_supplier=runtime_supplier_out,
        runtime_inputs=runtime_inputs_out,
    )


def _is_selectable(slot: str, row: FeatureRegistryEntryRow) -> bool:
    return row.status in {"active", "experimental"} and slot in row.enabled_for


def _is_experimental_trainable_feature_id(feature_id: str) -> bool:
    """Registry feature ids that may enter candidate/ablation training sets."""

    return feature_id.startswith("fe__") or feature_id.startswith("txn__") or feature_id.startswith("sess__")


def _ablation_groups_in_order(rows: tuple[FeatureRegistryEntryRow, ...]) -> tuple[str, ...]:
    """``group_*`` IDs that contribute at least one selectable experimental column for ablation."""

    ordered: list[str] = []
    seen: set[str] = set()
    for r in rows:
        if not r.group_id.startswith("group_"):
            continue
        if not _is_experimental_trainable_feature_id(r.feature_id):
            continue
        if not _is_selectable("ablation", r):
            continue
        if r.group_id not in seen:
            seen.add(r.group_id)
            ordered.append(r.group_id)
    return tuple(ordered)


def load_candidate_registry(path: Path | None = None) -> CandidateRegistrySnapshot:
    """Parse registry YAML; baseline columns are YAML order × selectable ``baseline`` slot."""

    p = (
        Path(path).resolve()
        if path is not None
        else default_registry_path()
    )
    if not p.is_file():
        raise FileNotFoundError(f"Candidate registry missing: {p}")
    blob = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(blob, dict):
        raise ValueError(f"Registry root must be a mapping, got {type(blob)} from {p}")
    ver = blob.get("registry_version")
    if not isinstance(ver, str) or not ver.strip():
        raise ValueError(f"registry_version must be a non-empty string in {p}")
    upd = blob.get("updated_at")
    if upd is not None and not isinstance(upd, str):
        raise TypeError(f"updated_at must be str or null in {p}")
    feats_raw = blob.get("features")
    if not isinstance(feats_raw, list) or not feats_raw:
        raise ValueError(f"features must be a non-empty list in {p}")
    rows_list = [_row_from_raw(i, it) for i, it in enumerate(feats_raw)]
    ids = [r.feature_id for r in rows_list]
    if len(ids) != len(set(ids)):
        dup = sorted({x for x in ids if ids.count(x) > 1})
        raise ValueError(f"Duplicate feature_id entries: {dup}")
    rows = tuple(rows_list)

    baseline_selected: list[str] = []
    for r in rows:
        if not _is_selectable("baseline", r):
            continue
        baseline_selected.append(r.feature_id)
    if not baseline_selected:
        raise ValueError(
            f"Registry {p} must declare at least one baseline feature "
            "(status active|experimental, enabled_for includes baseline).",
        )

    fe_trainable_order: list[str] = []
    for r in rows:
        if _is_experimental_trainable_feature_id(r.feature_id) and _is_selectable("candidate", r):
            fe_trainable_order.append(r.feature_id)
    exp_num = tuple(dict.fromkeys(fe_trainable_order))
    full = tuple(dict.fromkeys(tuple(baseline_selected) + tuple(exp_num)))

    group_order: dict[str, list[str]] = {}
    for r in rows:
        if r.group_id not in group_order:
            group_order[r.group_id] = []
        if r.feature_id not in group_order[r.group_id]:
            group_order[r.group_id].append(r.feature_id)
    fgt = {k: tuple(v) for k, v in sorted(group_order.items())}
    abi = _ablation_groups_in_order(rows)

    return CandidateRegistrySnapshot(
        registry_version=ver.strip(),
        updated_at=str(upd).strip() if isinstance(upd, str) and upd.strip() else None,
        path=p,
        rows=rows,
        model_feature_columns=tuple(baseline_selected),
        experimental_numeric_columns=exp_num,
        full_candidate_feature_columns=full,
        feature_group_tags=fgt,
        ablation_experimental_group_ids=abi,
    )


def baseline_features_for_main_trainer(snapshot: CandidateRegistrySnapshot) -> tuple[str, ...]:
    """Return ordered Step 5 feature columns for the main trainer."""

    cols = snapshot.model_feature_columns
    if not cols:
        raise ValueError(
            f"Candidate registry has empty baseline feature list (resolved_path={snapshot.path}). "
            "Fix feature_candidate_registry.yaml so at least one row is selectable for slot baseline.",
        )
    return cols


def candidate_features_for_group(snapshot: CandidateRegistrySnapshot, group_id: str, *, slot: str) -> tuple[str, ...]:
    """Return experimental feature ids in YAML order within ``group_id`` for ``slot``."""

    out: list[str] = []
    for r in snapshot.rows:
        if r.group_id != group_id or not _is_experimental_trainable_feature_id(r.feature_id):
            continue
        if _is_selectable(slot, r):
            out.append(r.feature_id)
    return tuple(out)


def load_registry_raw_feature_dicts(path: Path | None = None) -> list[dict[str, Any]]:
    """Return raw ``features`` list from registry YAML (for cadence audit metadata)."""

    p = Path(path).resolve() if path is not None else default_registry_path()
    if not p.is_file():
        raise FileNotFoundError(f"Candidate registry missing: {p}")
    blob = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(blob, dict):
        raise ValueError(f"Registry root must be a mapping, got {type(blob)} from {p}")
    feats_raw = blob.get("features")
    if not isinstance(feats_raw, list) or not feats_raw:
        raise ValueError(f"features must be a non-empty list in {p}")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(feats_raw):
        if not isinstance(item, dict):
            raise TypeError(f"features[{i}] must be a mapping, got {type(item)}")
        out.append(item)
    return out


def default_time_horizon_for_row(source: str, feature_id: str) -> tuple[str, str | None]:
    """Infer ``(time_horizon, max_lookback)`` for tooling / one-shot YAML bootstrap."""

    src = str(source).strip()
    fid = str(feature_id).strip()
    if src == "baseline_model":
        return "none", None
    if src == "feast_trial_1h":
        return "short_term", "PT1H"
    if src == "feast_slow_180d":
        return "long_term", "P180D"
    if src != "fe_derived":
        raise ValueError(f"unsupported source {source!r} for feature_id={feature_id!r}")

    clock: dict[str, tuple[str, str]] = {
        "fe__clock__hour_of_day": ("short_term", "PT1H"),
        "fe__clock__day_of_week": ("mid_term", "P7D"),
        "fe__clock__is_weekend": ("mid_term", "P7D"),
        "fe__clock__is_late_night": ("short_term", "PT1H"),
    }
    if fid in clock:
        return clock[fid]

    special: dict[str, tuple[str, str]] = {
        "fe__stake__wager_step_pct": ("short_term", "PT1H"),
        "fe__stake__wager_trend_slope__w1h": ("short_term", "PT1H"),
        "fe__stake__wager_last3_vs_prior3_ratio__w1h": ("short_term", "PT1H"),
        "fe__odds__payout_odds_step_ratio": ("short_term", "PT1H"),
        "fe__time_since_last_bet_sec": ("mid_term", "PT24H"),
        "fe__outcome__last_3_bets_loss_count": ("short_term", "PT1H"),
        "fe__outcome__consecutive_loss_streak": ("short_term", "PT1H"),
        "fe__outcome__loss_then_double_ratio__w1h": ("short_term", "PT1H"),
        "fe__outcome__wager_after_loss_step_ratio__w1h": ("short_term", "PT1H"),
        "fe__interarrival__lag2_sec": ("short_term", "PT1H"),
    }
    if fid in special:
        return special[fid]

    pairs: list[tuple[int, str]] = []
    for n_s, u in re.findall(r"w(\d+)(m|h|d)\b", fid):
        pairs.append((int(n_s), u))
    if "__today" in fid:
        pairs.append((1, "d"))

    if (
        not pairs
        and (
            fid.startswith("fe__session__")
            or fid.startswith("fe__tableswitch__")
        )
    ):
        return "short_term", "PT1H"

    if not pairs:
        return "mid_term", "P1D"

    best_td = pd.Timedelta(0)
    best_iso = "P1D"
    for n, u in pairs:
        if u == "m":
            td = pd.Timedelta(minutes=n)
            iso = f"PT{n}M"
        elif u == "h":
            td = pd.Timedelta(hours=n)
            iso = f"PT{n}H"
        else:
            td = pd.Timedelta(days=n)
            iso = f"P{n}D"
        if td >= best_td:
            best_td = td
            best_iso = iso
    exp_h = horizon_from_max_lookback_iso8601(best_iso)
    return exp_h, best_iso


def default_registry_path() -> Path:
    """Default ``feature_candidate_registry.yaml`` under packaged ``trainer_hightier/contracts``."""

    return (Path(__file__).resolve().parents[1] / "contracts" / "feature_candidate_registry.yaml").resolve()
