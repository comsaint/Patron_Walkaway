"""On-disk LibSVM helpers for A3 GBM bakeoff (XGBoost / CatBoost OOM mitigation).

Plan B+ exports rated-only train/valid/test LibSVM under ``DATA_DIR/export`` with
train instance weights in ``<train>.libsvm.weight`` (one float per line, text).
XGBoost does not reliably auto-load ``*.weight`` companions for ``DMatrix(uri)`` in
all builds; we materialise a float32 memmap sidecar for ``weight=`` without holding
the full vector in RAM. CatBoost cannot pass ``weight=`` when ``Pool`` is built from a
file URI; we call ``Pool.set_weight(memmap)`` after load. CatBoost's LibSVM loader
requires **1-based** feature indices; exports use **0-based** (LightGBM); we stream
rewrite into ``cache_dir`` and reuse while newer than the source.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

import numpy as np

from trainer.core import config as _cfg


@dataclass(frozen=True)
class GbmBakeoffLibSvmBundle:
    """Contract: same LibSVM layout as Plan B+ export (0-based sparse indices)."""

    train_libsvm: Path
    valid_libsvm: Path
    test_libsvm: Optional[Path]
    feature_names: Tuple[str, ...]
    train_row_count: int
    cache_dir: Path

    def cache_key_material(self) -> str:
        """Stable string for external-memory cache filenames (not security-sensitive)."""
        parts = [
            str(self.train_libsvm.resolve()),
            str(self.valid_libsvm.resolve()),
            str(self.test_libsvm.resolve()) if self.test_libsvm else "",
            str(int(self.train_row_count)),
            "|".join(self.feature_names),
        ]
        return "\n".join(parts)


def bakeoff_cache_dir(export_dir: Path, bundle: GbmBakeoffLibSvmBundle) -> Path:
    """Isolate XGBoost external-memory cache files per LibSVM + feature contract."""
    digest = hashlib.md5(bundle.cache_key_material().encode("utf-8")).hexdigest()[:16]
    return Path(export_dir) / ".gbm_bakeoff_cache" / digest


def count_nonempty_lines(path: Path) -> int:
    """Count non-empty lines in a text file without loading whole file into RAM."""
    n = 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.strip():
                n += 1
    return n


def _weight_txt_path(train_libsvm: Path) -> Path:
    return Path(str(train_libsvm) + ".weight")


def _weight_f32_path(train_libsvm: Path) -> Path:
    return Path(str(train_libsvm) + ".weight.f32")


def ensure_train_weight_f32_memmap(
    train_libsvm: Path,
    *,
    expected_rows: Optional[int] = None,
) -> Tuple[np.memmap, int]:
    """Return ``(memmap, n_rows)`` of train instance weights as float32.

    Builds ``<train>.libsvm.weight.f32`` from the text ``.weight`` file when missing
    or stale (mtime). Validates row count against LibSVM line count when provided.
    """
    w_txt = _weight_txt_path(train_libsvm)
    w_bin = _weight_f32_path(train_libsvm)
    if not w_txt.is_file():
        raise FileNotFoundError(f"Missing train weight file: {w_txt}")
    n_lib = expected_rows if expected_rows is not None else count_nonempty_lines(train_libsvm)
    if n_lib < 1:
        raise ValueError(f"train_libsvm has no rows: {train_libsvm}")
    need_rebuild = True
    if w_bin.is_file():
        try:
            st_txt = w_txt.stat().st_mtime_ns
            st_bin = w_bin.stat().st_mtime_ns
            if st_bin >= st_txt and w_bin.stat().st_size == n_lib * 4:
                need_rebuild = False
        except OSError:
            need_rebuild = True
    if need_rebuild:
        tmp = w_bin.with_suffix(w_bin.suffix + ".tmp")
        n_written = 0
        with open(w_txt, encoding="utf-8", errors="replace") as src, open(tmp, "wb") as dst:
            for line in src:
                tok = line.strip()
                if not tok:
                    continue
                try:
                    v = float(tok)
                except ValueError:
                    v = 1.0
                if not math.isfinite(v) or v < 0.0:
                    v = 0.0
                dst.write(struct.pack("<f", float(v)))
                n_written += 1
        if n_written != n_lib:
            tmp.unlink(missing_ok=True)
            raise ValueError(
                f"Train weight line count {n_written} != LibSVM rows {n_lib} "
                f"(train={train_libsvm}, weight={w_txt})"
            )
        os.replace(tmp, w_bin)
    mm = np.memmap(str(w_bin), dtype=np.float32, mode="r", shape=(n_lib,))
    return mm, n_lib


def xgboost_libsvm_uri(path: Path, *, external_memory: bool, cache_dir: Path) -> str:
    """Build XGBoost text URI for LibSVM; optional ``#cache`` suffix for external memory."""
    p = path.resolve().as_posix()
    base = f"{p}?format=libsvm"
    if not external_memory:
        return base
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe = path.name.replace(" ", "_").replace("/", "_")
    cache_file = cache_dir / f"{safe}.xgb_ext.cache"
    return f"{base}#{cache_file.as_posix()}"


def catboost_libsvm_uri(path: Path) -> str:
    """CatBoost Pool libsvm file URI (CatBoost docs: ``libsvm://`` prefix)."""
    return "libsvm://" + path.resolve().as_posix()


def _stream_libsvm_shift_feature_indices_plus_one(src: Path, dst: Path) -> None:
    """Rewrite LibSVM lines so each ``k:`` feature index becomes ``k+1`` (CatBoost vs LightGBM export)."""
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    n_line = 0
    with open(src, encoding="utf-8", errors="replace") as fi, open(tmp, "w", encoding="utf-8") as fo:
        for raw in fi:
            n_line += 1
            line = raw.strip()
            if not line:
                continue
            parts = line.split()
            if not parts:
                continue
            out_parts = [parts[0]]
            for tok in parts[1:]:
                if ":" not in tok:
                    raise ValueError(
                        f"LibSVM token without ':' (line {n_line}, file={src}): {tok!r}"
                    )
                k_s, v_s = tok.split(":", 1)
                try:
                    k = int(k_s)
                except ValueError as exc:
                    raise ValueError(
                        f"Non-integer feature index (line {n_line}, file={src}): {tok!r}"
                    ) from exc
                out_parts.append(f"{k + 1}:{v_s}")
            fo.write(" ".join(out_parts) + "\n")
    os.replace(tmp, dst)


def catboost_libsvm_path_one_based_cached(src: Path, cache_dir: Path) -> Path:
    """Return path to a 1-based LibSVM copy under ``cache_dir``, rebuilt when ``src`` is newer."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    dst = cache_dir / f"{src.stem}_cb_idx1{src.suffix}"
    need = True
    if dst.is_file():
        try:
            need = dst.stat().st_mtime < src.stat().st_mtime
        except OSError:
            need = True
    if need:
        _stream_libsvm_shift_feature_indices_plus_one(src, dst)
    return dst


def validate_bundle_paths(bundle: GbmBakeoffLibSvmBundle) -> None:
    """Raise with a concrete message when required bakeoff LibSVM inputs are missing."""
    if not bundle.train_libsvm.is_file():
        raise FileNotFoundError(f"Missing train LibSVM: {bundle.train_libsvm}")
    if not bundle.valid_libsvm.is_file():
        raise FileNotFoundError(f"Missing valid LibSVM: {bundle.valid_libsvm}")
    if bundle.test_libsvm is not None and not bundle.test_libsvm.is_file():
        raise FileNotFoundError(f"Missing test LibSVM: {bundle.test_libsvm}")


def _disk_has_strong_validation(n_y: int, y_val: Any) -> bool:
    """Mirror ``gbm_bakeoff._has_strong_validation`` without importing ``gbm_bakeoff``."""
    import pandas as pd

    min_rows = int(getattr(_cfg, "MIN_VALID_TEST_ROWS", 50))
    if n_y < min_rows or n_y < 1:
        return False
    ya = np.asarray(
        y_val.to_numpy(copy=False) if isinstance(y_val, pd.Series) else y_val,
        dtype=float,
    ).reshape(-1)
    if int(np.isnan(ya).sum()) != 0:
        return False
    if int(np.sum(ya == 1.0)) < 1:
        return False
    if int(np.sum(ya == 0.0)) < 1:
        return False
    return True


class XGBoostBoosterDiskClassifier:
    """Sklearn-like wrapper: ``booster_`` fast path + ``predict_proba`` for batched rows."""

    def __init__(
        self,
        booster: Any,
        *,
        feature_names: Sequence[str],
        valid_libsvm_uri: str,
        test_libsvm_uri: Optional[str],
    ) -> None:
        self._Booster = booster
        self.n_features_in_ = int(len(feature_names))
        self._feature_names = list(feature_names)
        self.classes_ = np.array([0, 1], dtype=np.int64)
        self._valid_libsvm_uri = valid_libsvm_uri
        self._test_libsvm_uri = test_libsvm_uri

    @property
    def booster_(self) -> Any:
        """Lower-case alias expected by some trainer helpers."""
        return self._Booster

    @property
    def feature_importances_(self) -> list[float]:
        """Gain vector aligned to ``feature_names`` (for ``_compute_feature_importance``)."""
        sc = self._Booster.get_score(importance_type="gain")
        arr = np.zeros(self.n_features_in_, dtype=float)
        for k, v in sc.items():
            if isinstance(k, str) and k.startswith("f"):
                try:
                    idx = int(k[1:])
                except ValueError:
                    continue
                if 0 <= idx < len(arr):
                    arr[idx] = float(v)
        return arr.tolist()

    def predict_proba(self, X: Any) -> np.ndarray:
        """Dense-matrix predict (callers pass ``pd.DataFrame`` slices)."""
        import xgboost as xgb

        if X is None or (hasattr(X, "empty") and X.empty):
            return np.zeros((0, 2), dtype=np.float64)
        arr = np.ascontiguousarray(X.to_numpy(dtype=np.float32, copy=True))
        dm = xgb.DMatrix(arr, feature_names=self._feature_names)
        pos = np.asarray(self._Booster.predict(dm), dtype=np.float64).reshape(-1)
        neg = 1.0 - pos
        return np.column_stack((neg, pos))

    def predict_val_scores_from_libsvm(self) -> np.ndarray:
        """Validation positive-class scores without a dense ``DataFrame``."""
        import xgboost as xgb

        dm = xgb.DMatrix(self._valid_libsvm_uri, feature_names=self._feature_names)
        return np.asarray(self._Booster.predict(dm), dtype=np.float64).reshape(-1)

    def predict_test_scores_from_libsvm(self) -> Optional[np.ndarray]:
        """Test positive-class scores from on-disk LibSVM."""
        import xgboost as xgb

        if not self._test_libsvm_uri:
            return None
        dm = xgb.DMatrix(self._test_libsvm_uri, feature_names=self._feature_names)
        return np.asarray(self._Booster.predict(dm), dtype=np.float64).reshape(-1)


def _xgb_hp_to_train_params(hp: Mapping[str, Any]) -> dict[str, Any]:
    """Map sklearn-style XGBoost hyperparams to ``xgboost.train`` params."""
    raw = dict(hp)
    if "learning_rate" in raw:
        raw["eta"] = float(raw.pop("learning_rate"))
    if "random_state" in raw:
        raw["seed"] = int(raw.pop("random_state"))
    if "n_jobs" in raw:
        nj = raw.pop("n_jobs")
        if nj == -1 or nj is None:
            raw["nthread"] = max(1, (os.cpu_count() or 4))
        else:
            try:
                raw["nthread"] = max(1, int(nj))
            except (TypeError, ValueError):
                raw["nthread"] = 1
    for drop_key in ("n_estimators", "verbosity", "scale_pos_weight"):
        raw.pop(drop_key, None)
    raw.setdefault("objective", "binary:logistic")
    raw.setdefault("eval_metric", "logloss")
    raw.setdefault("tree_method", "hist")
    return raw


def train_xgboost_from_libsvm_disk(
    bundle: GbmBakeoffLibSvmBundle,
    hp: Mapping[str, Any],
    *,
    y_val: Any,
    backend_runtime_params: Optional[Mapping[str, Any]] = None,
    val_dec026_window_hours: Optional[float] = None,
    val_dec026_min_alerts_per_hour: Optional[float] = None,
    use_external_memory: bool = False,
) -> Tuple[Any, dict[str, Any]]:
    """Train XGBoost from Plan B+ LibSVM + float32 weight memmap."""
    import pandas as pd
    import xgboost as xgb

    # Lazy import after ``gbm_bakeoff`` is fully loaded (avoids import cycles at startup).
    from trainer.training.gbm_bakeoff import _val_block_from_scores

    validate_bundle_paths(bundle)
    w_mm, _n = ensure_train_weight_f32_memmap(
        bundle.train_libsvm, expected_rows=bundle.train_row_count
    )
    cache_dir = Path(bundle.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    device = str((backend_runtime_params or {}).get("device", "cpu")).lower()
    ext_ok = bool(use_external_memory) and not device.startswith("cuda")
    train_uri = xgboost_libsvm_uri(
        bundle.train_libsvm, external_memory=ext_ok, cache_dir=cache_dir
    )
    valid_uri = xgboost_libsvm_uri(
        bundle.valid_libsvm, external_memory=False, cache_dir=cache_dir
    )
    test_uri = (
        xgboost_libsvm_uri(bundle.test_libsvm, external_memory=False, cache_dir=cache_dir)
        if bundle.test_libsvm is not None
        else None
    )
    dtrain = xgb.DMatrix(
        train_uri,
        weight=w_mm,
        feature_names=list(bundle.feature_names),
    )
    dvalid = xgb.DMatrix(valid_uri, feature_names=list(bundle.feature_names))
    x_hp = dict(hp)
    if backend_runtime_params:
        x_hp.update(dict(backend_runtime_params))
    n_rounds = int(x_hp.pop("n_estimators"))
    params = _xgb_hp_to_train_params(x_hp)
    yv = y_val if isinstance(y_val, pd.Series) else pd.Series(np.asarray(y_val).reshape(-1))
    has_val = _disk_has_strong_validation(len(yv), yv)
    if has_val:
        booster = xgb.train(
            params,
            dtrain,
            num_boost_round=max(1, n_rounds),
            evals=[(dvalid, "valid")],
            early_stopping_rounds=50,
            verbose_eval=False,
        )
        val_scores = np.asarray(booster.predict(dvalid), dtype=np.float64).reshape(-1)
    else:
        booster = xgb.train(
            params,
            dtrain,
            num_boost_round=max(1, n_rounds),
            verbose_eval=False,
        )
        val_scores = np.zeros(len(yv), dtype=np.float64)
    metrics = _val_block_from_scores(
        yv,
        val_scores,
        hp,
        label="rated_xgboost",
        val_dec026_window_hours=val_dec026_window_hours,
        val_dec026_min_alerts_per_hour=val_dec026_min_alerts_per_hour,
    )
    model = XGBoostBoosterDiskClassifier(
        booster,
        feature_names=bundle.feature_names,
        valid_libsvm_uri=valid_uri,
        test_libsvm_uri=test_uri,
    )
    setattr(model, "a3_final_fit_mode", "libsvm_disk")
    return model, metrics


def train_catboost_from_libsvm_disk(
    bundle: GbmBakeoffLibSvmBundle,
    hp: Mapping[str, Any],
    *,
    y_val: Any,
    backend_runtime_params: Optional[Mapping[str, Any]] = None,
    val_dec026_window_hours: Optional[float] = None,
    val_dec026_min_alerts_per_hour: Optional[float] = None,
    quantize_first: bool = False,
) -> Tuple[Any, dict[str, Any]]:
    """Train CatBoost from LibSVM URI + instance weight memmap."""
    import pandas as pd
    from catboost import CatBoostClassifier, Pool

    from trainer.training.gbm_bakeoff import _val_block_from_scores
    from trainer.training.trainer import _sanitize_catboost_params_for_runtime

    validate_bundle_paths(bundle)
    w_mm, _n = ensure_train_weight_f32_memmap(
        bundle.train_libsvm, expected_rows=bundle.train_row_count
    )
    cache_dir = Path(bundle.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    train_1 = catboost_libsvm_path_one_based_cached(bundle.train_libsvm, cache_dir)
    valid_1 = catboost_libsvm_path_one_based_cached(bundle.valid_libsvm, cache_dir)
    test_1 = (
        catboost_libsvm_path_one_based_cached(bundle.test_libsvm, cache_dir)
        if bundle.test_libsvm is not None
        else None
    )
    train_uri = catboost_libsvm_uri(train_1)
    valid_uri = catboost_libsvm_uri(valid_1)
    c_hp = dict(hp)
    if backend_runtime_params:
        c_hp.update(dict(backend_runtime_params))
    c_hp.pop("class_weights", None)
    iterations = int(c_hp.pop("iterations"))
    early = int(c_hp.pop("early_stopping_rounds"))
    c_hp = _sanitize_catboost_params_for_runtime(c_hp)
    train_pool = Pool(data=train_uri)
    train_pool.set_weight(w_mm)
    val_pool = Pool(data=valid_uri)
    if quantize_first:
        try:
            from catboost import quantize as cb_quantize

            train_pool = cb_quantize(train_pool)
            val_pool = cb_quantize(val_pool)
        except Exception as exc:
            raise RuntimeError(f"CatBoost quantize() failed: {exc}") from exc
    model = CatBoostClassifier(iterations=iterations, **c_hp)
    yv = y_val if isinstance(y_val, pd.Series) else pd.Series(np.asarray(y_val).reshape(-1))
    has_val = _disk_has_strong_validation(len(yv), yv)
    if has_val:
        model.fit(
            train_pool,
            eval_set=val_pool,
            early_stopping_rounds=early,
            verbose=False,
        )
        val_scores = np.asarray(model.predict_proba(val_pool)[:, 1], dtype=float)
    else:
        model.fit(train_pool, verbose=False)
        val_scores = np.zeros(len(yv), dtype=float)
    metrics = _val_block_from_scores(
        yv,
        val_scores,
        hp,
        label="rated_catboost",
        val_dec026_window_hours=val_dec026_window_hours,
        val_dec026_min_alerts_per_hour=val_dec026_min_alerts_per_hour,
    )
    setattr(model, "_gbm_bakeoff_valid_libsvm_uri", valid_uri)
    setattr(
        model,
        "_gbm_bakeoff_test_libsvm_uri",
        catboost_libsvm_uri(test_1) if test_1 is not None else None,
    )
    setattr(model, "a3_final_fit_mode", "libsvm_disk")
    return model, metrics
