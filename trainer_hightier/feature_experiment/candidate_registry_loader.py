"""Load and validate ``trainer_hightier/contracts/feature_candidate_registry.yaml``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

_REGISTRY_STATUSES: Final[frozenset[str]] = frozenset({"active", "disabled", "experimental"})
_REGISTRY_SLOTS: Final[frozenset[str]] = frozenset({"baseline", "candidate", "ablation"})


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


@dataclass(frozen=True)
class CandidateRegistrySnapshot:
    """Validated registry; drives selection in :mod:`feature_registry`."""

    registry_version: str
    updated_at: str | None
    path: Path
    rows: tuple[FeatureRegistryEntryRow, ...]
    model_feature_columns: tuple[str, ...]
    experimental_numeric_columns: tuple[str, ...]
    full_candidate_feature_columns: tuple[str, ...]
    feature_group_tags: dict[str, tuple[str, ...]]
    ablation_experimental_group_ids: tuple[str, ...]


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
    return FeatureRegistryEntryRow(
        feature_id=fid.strip(),
        group_id=gid.strip(),
        source=src.strip(),
        status=st,
        enabled_for=slots,
        drop_reason_code=str(drc).strip() if isinstance(drc, str) and drc.strip() else None,
        semantic_owner=str(so).strip() if isinstance(so, str) and so.strip() else None,
        first_seen_experiment=str(fse).strip() if isinstance(fse, str) and fse.strip() else None,
        last_updated_experiment=str(lue).strip() if isinstance(lue, str) and lue.strip() else None,
        note=str(note).strip() if isinstance(note, str) and note.strip() else None,
    )


def _is_selectable(slot: str, row: FeatureRegistryEntryRow) -> bool:
    return row.status in {"active", "experimental"} and slot in row.enabled_for


def _ablation_groups_in_order(rows: tuple[FeatureRegistryEntryRow, ...]) -> tuple[str, ...]:
    """``group_*`` IDs that contribute at least one selectable ``fe__*`` column for ablation."""

    ordered: list[str] = []
    seen: set[str] = set()
    for r in rows:
        if not r.group_id.startswith("group_"):
            continue
        if not r.feature_id.startswith("fe__"):
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
        else (Path(__file__).resolve().parents[1] / "contracts" / "feature_candidate_registry.yaml")
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
        if r.feature_id.startswith("fe__") and _is_selectable("candidate", r):
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
    """Return ordered Step 5 feature columns for the main trainer.

    Uses the same rule as experiment baseline: ``status`` is ``active`` or ``experimental`` and
    ``baseline`` is listed in ``enabled_for``; order follows YAML declaration.
    """

    cols = snapshot.model_feature_columns
    if not cols:
        raise ValueError(
            f"Candidate registry has empty baseline feature list (resolved_path={snapshot.path}). "
            "Fix feature_candidate_registry.yaml so at least one row is selectable for slot baseline.",
        )
    return cols


def candidate_features_for_group(snapshot: CandidateRegistrySnapshot, group_id: str, *, slot: str) -> tuple[str, ...]:
    """Return ``fe__*`` ids in YAML order within ``group_id`` that are selectable for ``slot``."""

    out: list[str] = []
    for r in snapshot.rows:
        if r.group_id != group_id or not r.feature_id.startswith("fe__"):
            continue
        if _is_selectable(slot, r):
            out.append(r.feature_id)
    return tuple(out)



def default_registry_path() -> Path:
    """Default ``feature_candidate_registry.yaml`` under ``trainer_hightier/contracts``."""

    return (Path(__file__).resolve().parents[1] / "contracts" / "feature_candidate_registry.yaml").resolve()
