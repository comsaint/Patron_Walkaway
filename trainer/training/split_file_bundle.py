"""File-backed rated split contract (GitHub #25).

Defines validation helpers and optional manifest I/O for train/valid/test LibSVM
splits used when ``TRAINER_FILE_BACKED_STRICT`` is enabled.

Status (#25, rated LibSVM path — implementation snapshot)
--------------------------------------------------------
In-tree: strict gate + path/manifest contract (this module); ``trainer.py`` wires
strict Optuna on-disk (LightGBM), file-backed train∪valid refit, training-metrics
provenance fields, and fail-fast train metrics when LibSVM predict is required;
``gbm_bakeoff.py`` re-raises on CatBoost/XGBoost disk-train failure under strict
(no silent in-memory fallback). Rollout remains env-driven (set
``TRAINER_FILE_BACKED_STRICT`` in CI/prod when ready). With strict + LibSVM bundle,
A3 optional backends use disk Optuna trials, disk train∪valid refit (CatBoost/XGBoost),
and Phase E streaming is forced on when LibSVM paths are present.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

MANIFEST_FILENAME = "trainer_split_manifest.v1.json"
MANIFEST_KIND = "trainer_rated_split_file_bundle_v1"


def forbid_file_backed_strict_dense_predict(
    *,
    role: str,
    detail: str = "",
) -> None:
    """Fail fast when strict file-backed mode would otherwise use dense-matrix predict."""
    if not trainer_file_backed_strict_enabled():
        return
    msg = (
        "TRAINER_FILE_BACKED_STRICT: LibSVM bundle is active but "
        f"{role} scores would use in-memory dense predict (forbidden). "
        "Enable Phase E streaming (GBM_BAKEOFF_PREDICT_STREAMING) or ensure "
        "on-disk predict (e.g. xgboost_libsvm_uri / catboost_libsvm_pool_uri) works."
    )
    if detail:
        msg = f"{msg} Detail: {detail}"
    raise RuntimeError(msg)


def trainer_file_backed_strict_enabled() -> bool:
    """Return True when strict file-backed training is required (Issue #25)."""
    v = (os.environ.get("TRAINER_FILE_BACKED_STRICT") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def validate_libsvm_paths_exist(
    train_libsvm: Path,
    valid_libsvm: Path,
    *,
    test_libsvm: Optional[Path] = None,
) -> None:
    """Fail fast if expected LibSVM paths are missing."""
    if not train_libsvm.is_file():
        raise FileNotFoundError(
            f"TRAINER_FILE_BACKED_STRICT: missing train LibSVM: {train_libsvm}"
        )
    if not valid_libsvm.is_file():
        raise FileNotFoundError(
            f"TRAINER_FILE_BACKED_STRICT: missing valid LibSVM: {valid_libsvm}"
        )
    w_train = Path(str(train_libsvm) + ".weight")
    if not w_train.is_file():
        raise FileNotFoundError(
            f"TRAINER_FILE_BACKED_STRICT: missing train weights {w_train} "
            "(Plan B+ export must write .weight beside train LibSVM)."
        )
    if test_libsvm is not None and not test_libsvm.is_file():
        raise FileNotFoundError(
            f"TRAINER_FILE_BACKED_STRICT: missing test LibSVM: {test_libsvm}"
        )


def write_split_manifest(
    export_dir: Path,
    *,
    train_libsvm: Path,
    valid_libsvm: Path,
    test_libsvm: Optional[Path],
    feature_columns: Sequence[str],
    train_row_count: int,
    valid_row_count: int,
    test_row_count: Optional[int] = None,
    feature_index_base: str = "zero",
    trainer_file_backed_strict_effective: Optional[bool] = None,
    backends_contract: Optional[Sequence[str]] = None,
) -> Path:
    """Write a JSON manifest next to LibSVM exports for reproducibility / CI checks.

    ``schema_version`` 2 adds optional #25 contract fields; readers should use
    :func:`dict.get` for backward compatibility with older manifests.

    Returns
    -------
    Path
        Path to the written manifest file.
    """
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    _strict = (
        bool(trainer_file_backed_strict_effective)
        if trainer_file_backed_strict_effective is not None
        else trainer_file_backed_strict_enabled()
    )
    payload: Dict[str, Any] = {
        "artifact_kind": MANIFEST_KIND,
        "schema_version": 2,
        "train_libsvm": str(Path(train_libsvm).resolve()),
        "valid_libsvm": str(Path(valid_libsvm).resolve()),
        "test_libsvm": str(Path(test_libsvm).resolve()) if test_libsvm else None,
        "feature_columns": [str(c) for c in feature_columns],
        "train_row_count": int(train_row_count),
        "valid_row_count": int(valid_row_count),
        "test_row_count": int(test_row_count) if test_row_count is not None else None,
        "feature_index_base": str(feature_index_base),
        "trainer_file_backed_strict_effective": bool(_strict),
        "backends_contract": [str(b) for b in backends_contract]
        if backends_contract
        else ["lightgbm", "catboost", "xgboost"],
    }
    out = export_dir / MANIFEST_FILENAME
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def read_split_manifest(export_dir: Path) -> Dict[str, Any]:
    """Load manifest from *export_dir*; raises if missing."""
    p = Path(export_dir) / MANIFEST_FILENAME
    if not p.is_file():
        raise FileNotFoundError(f"Split manifest not found: {p}")
    return dict(json.loads(p.read_text(encoding="utf-8")))


def merge_libsvm_files(dest: Path, parts: Sequence[Path]) -> int:
    """Concatenate LibSVM text files into *dest* (streaming; one line at a time).

    Returns
    -------
    int
        Total non-empty lines written.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    n_out = 0
    with open(tmp, "w", encoding="utf-8") as fo:
        for src in parts:
            sp = Path(src)
            if not sp.is_file():
                raise FileNotFoundError(f"merge_libsvm_files: missing {sp}")
            with open(sp, encoding="utf-8", errors="replace") as fi:
                for line in fi:
                    if line.strip():
                        fo.write(line if line.endswith("\n") else line + "\n")
                        n_out += 1
    os.replace(tmp, dest)
    return n_out


def merge_train_valid_weight_files(
    dest: Path,
    *,
    train_weight_txt: Path,
    valid_libsvm: Path,
) -> None:
    """Append ``1.0`` weights for each valid LibSVM row after *train_weight_txt* lines."""
    from trainer.training.gbm_bakeoff_disk import count_nonempty_lines

    dest = Path(dest)
    n_valid = count_nonempty_lines(Path(valid_libsvm))
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tw = Path(train_weight_txt)
    if not tw.is_file():
        raise FileNotFoundError(f"Missing train weight file: {tw}")
    with open(tmp, "w", encoding="utf-8") as fo:
        with open(tw, encoding="utf-8", errors="replace") as fi:
            for line in fi:
                if line.strip():
                    fo.write(line if line.endswith("\n") else line + "\n")
        for _ in range(int(n_valid)):
            fo.write("1.0\n")
    os.replace(tmp, dest)
