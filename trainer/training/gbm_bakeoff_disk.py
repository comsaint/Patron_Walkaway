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
from typing import Any, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np

from trainer.core import config as _cfg

_PHASE_E_BATCH_CAP = 2_000_000
_PHASE_E_BATCH_FLOOR = 100
_PHASE_E_DEFAULT_BATCH = 50_000


def phase_e_predict_streaming_enabled() -> bool:
    """Phase E: batch LibSVM lines for predict (lower peak RAM than full-file DMatrix/Pool)."""
    cfg_v = getattr(_cfg, "GBM_BAKEOFF_PREDICT_STREAMING", None)
    if cfg_v is not None:
        return bool(cfg_v)
    raw = (os.getenv("GBM_BAKEOFF_PREDICT_STREAMING") or "").strip().lower()
    return bool(raw) and raw in ("1", "true", "t", "yes", "y")


def phase_e_score_memmap_enabled() -> bool:
    """Phase E: write streamed positive-class scores to float32 memmap under bakeoff cache_dir."""
    cfg_v = getattr(_cfg, "GBM_BAKEOFF_SCORE_MEMMAP", None)
    if cfg_v is not None:
        return bool(cfg_v)
    raw = (os.getenv("GBM_BAKEOFF_SCORE_MEMMAP") or "").strip().lower()
    return bool(raw) and raw in ("1", "true", "t", "yes", "y")


def phase_e_ap_mode() -> str:
    """Train AP aggregation mode for Phase E metrics: ``legacy|approx_histogram|exact_external_sort``."""
    cfg_v = getattr(_cfg, "GBM_BAKEOFF_AP_MODE", None)
    if isinstance(cfg_v, str) and cfg_v.strip():
        s = cfg_v.strip().lower()
        if s in ("approx_histogram", "exact_external_sort", "legacy"):
            return s
    v = (os.getenv("GBM_BAKEOFF_AP_MODE") or "").strip().lower()
    if v in ("approx_histogram", "exact_external_sort", "legacy"):
        return v
    return "legacy"


def phase_e_predict_batch_rows() -> int:
    """Rows per LibSVM batch for Phase E streaming predict (bounded)."""
    cfg_n = getattr(_cfg, "GBM_BAKEOFF_PREDICT_BATCH_ROWS", None)
    if cfg_n is not None:
        try:
            n = int(cfg_n)
        except (TypeError, ValueError):
            n = _PHASE_E_DEFAULT_BATCH
    else:
        raw = os.getenv("GBM_BAKEOFF_PREDICT_BATCH_ROWS")
        if raw is None or not str(raw).strip():
            n = _PHASE_E_DEFAULT_BATCH
        else:
            try:
                n = int(str(raw).strip())
            except ValueError:
                n = _PHASE_E_DEFAULT_BATCH
    return max(_PHASE_E_BATCH_FLOOR, min(int(n), _PHASE_E_BATCH_CAP))


def iter_libsvm_nonempty_line_batches(path: Path, batch_rows: int) -> Iterator[List[str]]:
    """Yield batches of non-empty LibSVM text lines (no trailing newlines in each line string)."""
    br = max(_PHASE_E_BATCH_FLOOR, min(int(batch_rows), _PHASE_E_BATCH_CAP))
    batch: List[str] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            batch.append(line)
            if len(batch) >= br:
                yield batch
                batch = []
    if batch:
        yield batch


def _xgb_booster_predict_libsvm_line_batches(
    booster: Any,
    feature_names: Sequence[str],
    line_batches: Iterator[List[str]],
    cache_dir: Path,
) -> np.ndarray:
    """Concatenate positive-class predictions for each LibSVM line batch (0-based indices)."""
    import xgboost as xgb

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    chunk_path = cache_dir / f"_phase_e_xgb_chunk_{os.getpid()}.libsvm"
    parts: list[np.ndarray] = []
    for lines in line_batches:
        if not lines:
            continue
        blob = "\n".join(lines) + "\n"
        chunk_path.write_text(blob, encoding="utf-8")
        uri = str(chunk_path.resolve()) + "?format=libsvm"
        dm = xgb.DMatrix(uri, feature_names=list(feature_names))
        parts.append(np.asarray(booster.predict(dm), dtype=np.float32).reshape(-1))
    try:
        chunk_path.unlink(missing_ok=True)
    except OSError:
        pass
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts, axis=0)


def _catboost_predict_libsvm_line_batches(
    model: Any,
    line_batches: Iterator[List[str]],
    tmp_dir: Path,
) -> np.ndarray:
    """CatBoost: materialise each 1-based LibSVM batch to a temp file and predict_proba."""
    from catboost import Pool

    tmp_dir.mkdir(parents=True, exist_ok=True)
    parts: list[np.ndarray] = []
    for idx, lines in enumerate(line_batches):
        if not lines:
            continue
        chunk_path = tmp_dir / f"_phase_e_cb_{os.getpid()}_{idx}.libsvm"
        try:
            chunk_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            uri = catboost_libsvm_uri(chunk_path)
            parts.append(np.asarray(model.predict_proba(Pool(uri))[:, 1], dtype=np.float32).reshape(-1))
        finally:
            chunk_path.unlink(missing_ok=True)
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts, axis=0)


def predict_positive_scores_phase_e_libsvm(
    *,
    backend: str,
    model: Any,
    libsvm_path: Path,
    feature_names: Sequence[str],
    cache_dir: Path,
    batch_rows: int,
    use_memmap: bool,
    role: str,
) -> Tuple[Union[np.ndarray, np.memmap], dict[str, Any]]:
    """Stream-predict positive-class scores from LibSVM; optional float32 memmap sink.

    Returns ``(scores, metadata)``. ``scores`` is ``float32`` ndarray or read-only memmap
    of length = non-empty LibSVM lines in ``libsvm_path``.
    """
    if not libsvm_path.is_file():
        raise FileNotFoundError(f"Phase E LibSVM missing: {libsvm_path}")
    br = max(_PHASE_E_BATCH_FLOOR, min(int(batch_rows), _PHASE_E_BATCH_CAP))
    meta: dict[str, Any] = {
        "a3_score_compute_mode": "libsvm_streaming",
        "a3_predict_batch_rows": int(br),
        "a3_score_dtype": "float32",
        "a3_phase_e_role": str(role),
        "a3_phase_e_backend": str(backend).lower(),
    }
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    backend_n = str(backend or "").strip().lower()

    if backend_n == "xgboost":
        booster = getattr(model, "booster_", None) or getattr(model, "_Booster", None)
        if booster is None:
            raise RuntimeError("Phase E xgboost: model has no booster_")
        n_rows = int(count_nonempty_lines(libsvm_path))

        if use_memmap:
            out_path = cache_dir / f".phase_e_scores_{libsvm_path.stem}_{role}_{os.getpid()}.f32"
            meta["a3_score_sink"] = "memmap"
            meta["a3_score_memmap_path"] = str(out_path.resolve())
            mm = np.memmap(str(out_path), dtype=np.float32, mode="w+", shape=(n_rows,))
            offset = 0
            _chunk_p = cache_dir / f"_phase_e_xgb_chunk_{os.getpid()}.libsvm"
            for lines in iter_libsvm_nonempty_line_batches(libsvm_path, br):
                if not lines:
                    continue
                blob = "\n".join(lines) + "\n"
                import xgboost as xgb

                _chunk_p.write_text(blob, encoding="utf-8")
                uri = str(_chunk_p.resolve()) + "?format=libsvm"
                dm = xgb.DMatrix(uri, feature_names=list(feature_names))
                chunk = np.asarray(booster.predict(dm), dtype=np.float32).reshape(-1)
                n = len(chunk)
                mm[offset : offset + n] = chunk
                offset += n
            mm.flush()
            ro = np.memmap(str(out_path), dtype=np.float32, mode="r", shape=(n_rows,))
            meta["a3_score_memmap_used"] = True
            try:
                _chunk_p.unlink(missing_ok=True)
            except OSError:
                pass
            return ro, meta

        meta["a3_score_sink"] = "memory"
        meta["a3_score_memmap_used"] = False
        arr = _xgb_booster_predict_libsvm_line_batches(
            booster,
            feature_names,
            iter_libsvm_nonempty_line_batches(libsvm_path, br),
            cache_dir,
        )
        if int(arr.shape[0]) != int(n_rows):
            raise ValueError(
                f"Phase E xgboost score row count {arr.shape[0]} != LibSVM lines {n_rows} ({libsvm_path})"
            )
        return arr, meta

    if backend_n == "catboost":
        cb_path, _hit = catboost_libsvm_path_one_based_cached(libsvm_path, cache_dir)
        n_rows = int(count_nonempty_lines(cb_path))
        tmp_dir = cache_dir / f"_phase_e_cb_tmp_{os.getpid()}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            if use_memmap:
                out_path = cache_dir / f".phase_e_scores_{libsvm_path.stem}_{role}_{os.getpid()}.f32"
                meta["a3_score_sink"] = "memmap"
                meta["a3_score_memmap_path"] = str(out_path.resolve())
                mm = np.memmap(str(out_path), dtype=np.float32, mode="w+", shape=(n_rows,))
                offset = 0
                for idx, lines in enumerate(iter_libsvm_nonempty_line_batches(cb_path, br)):
                    if not lines:
                        continue
                    chunk_path = tmp_dir / f"chunk_{idx}.libsvm"
                    chunk_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    try:
                        from catboost import Pool

                        uri = catboost_libsvm_uri(chunk_path)
                        chunk = np.asarray(
                            model.predict_proba(Pool(uri))[:, 1], dtype=np.float32
                        ).reshape(-1)
                    finally:
                        chunk_path.unlink(missing_ok=True)
                    n = len(chunk)
                    mm[offset : offset + n] = chunk
                    offset += n
                mm.flush()
                try:
                    tmp_dir.rmdir()
                except OSError:
                    pass
                ro = np.memmap(str(out_path), dtype=np.float32, mode="r", shape=(n_rows,))
                meta["a3_score_memmap_used"] = True
                return ro, meta

            meta["a3_score_sink"] = "memory"
            meta["a3_score_memmap_used"] = False
            arr = _catboost_predict_libsvm_line_batches(
                model, iter_libsvm_nonempty_line_batches(cb_path, br), tmp_dir
            )
            try:
                tmp_dir.rmdir()
            except OSError:
                pass
            if int(arr.shape[0]) != int(n_rows):
                raise ValueError(
                    f"Phase E catboost score row count {arr.shape[0]} != LibSVM lines {n_rows} ({cb_path})"
                )
            return arr, meta
        finally:
            if tmp_dir.is_dir():
                for p in tmp_dir.glob("*"):
                    p.unlink(missing_ok=True)
                try:
                    tmp_dir.rmdir()
                except OSError:
                    pass

    raise ValueError(f"Phase E streaming not supported for backend={backend!r}")


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


def catboost_libsvm_path_one_based_cached(src: Path, cache_dir: Path) -> Tuple[Path, bool]:
    """Return ``(path, cache_hit)`` for a 1-based LibSVM copy under ``cache_dir``.

    ``cache_hit`` is True when an on-disk cached copy was reused without rewrite.
    """
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
        return dst, False
    return dst, True


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
        train_libsvm_uri: Optional[str] = None,
    ) -> None:
        self._Booster = booster
        self.n_features_in_ = int(len(feature_names))
        self._feature_names = list(feature_names)
        self.classes_ = np.array([0, 1], dtype=np.int64)
        self._valid_libsvm_uri = valid_libsvm_uri
        self._test_libsvm_uri = test_libsvm_uri
        self._train_libsvm_uri = train_libsvm_uri

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

    def predict_train_scores_from_libsvm(self) -> np.ndarray:
        """Train positive-class scores from on-disk LibSVM (same URI used for ``DMatrix`` training)."""
        import xgboost as xgb

        if not self._train_libsvm_uri:
            raise RuntimeError("predict_train_scores_from_libsvm: train_libsvm_uri not set")
        dm = xgb.DMatrix(self._train_libsvm_uri, feature_names=self._feature_names)
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
        train_libsvm_uri=train_uri,
    )
    setattr(model, "a3_final_fit_mode", "libsvm_disk")
    metrics["a3_xgboost_external_memory_train"] = bool(ext_ok)
    metrics["a3_xgboost_libsvm_train_uri_has_ext_cache"] = bool("#" in train_uri)
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
    train_1, hit_train = catboost_libsvm_path_one_based_cached(bundle.train_libsvm, cache_dir)
    valid_1, hit_valid = catboost_libsvm_path_one_based_cached(bundle.valid_libsvm, cache_dir)
    test_1: Optional[Path]
    hit_test: Optional[bool]
    if bundle.test_libsvm is not None:
        test_1, hit_test = catboost_libsvm_path_one_based_cached(bundle.test_libsvm, cache_dir)
    else:
        test_1, hit_test = None, None
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
    setattr(model, "_gbm_bakeoff_train_libsvm_uri", train_uri)
    setattr(
        model,
        "_gbm_bakeoff_test_libsvm_uri",
        catboost_libsvm_uri(test_1) if test_1 is not None else None,
    )
    setattr(model, "a3_final_fit_mode", "libsvm_disk")
    metrics["a3_catboost_libsvm_cache_hit_train"] = bool(hit_train)
    metrics["a3_catboost_libsvm_cache_hit_valid"] = bool(hit_valid)
    if hit_test is not None:
        metrics["a3_catboost_libsvm_cache_hit_test"] = bool(hit_test)
    metrics["a3_catboost_quantize_first"] = bool(quantize_first)
    return model, metrics


def libsvm_bundle_for_a3_hpo(
    train_libsvm: Path,
    valid_libsvm: Path,
    *,
    train_row_count: int,
    feature_names: Sequence[str],
    test_libsvm: Optional[Path] = None,
) -> GbmBakeoffLibSvmBundle:
    """Build :class:`GbmBakeoffLibSvmBundle` for A3 / Optuna disk trials (same layout as Step 9)."""
    _tmp = GbmBakeoffLibSvmBundle(
        train_libsvm=Path(train_libsvm),
        valid_libsvm=Path(valid_libsvm),
        test_libsvm=Path(test_libsvm) if test_libsvm is not None else None,
        feature_names=tuple(str(x) for x in feature_names),
        train_row_count=int(train_row_count),
        cache_dir=Path(train_libsvm).parent,
    )
    return GbmBakeoffLibSvmBundle(
        train_libsvm=_tmp.train_libsvm,
        valid_libsvm=_tmp.valid_libsvm,
        test_libsvm=_tmp.test_libsvm,
        feature_names=_tmp.feature_names,
        train_row_count=_tmp.train_row_count,
        cache_dir=bakeoff_cache_dir(Path(train_libsvm).parent, _tmp),
    )


def hpo_trial_val_scores_xgboost_from_libsvm(
    params: Mapping[str, Any],
    bundle: GbmBakeoffLibSvmBundle,
    y_val: Any,
    *,
    backend_runtime_params: Optional[Mapping[str, Any]] = None,
    use_external_memory: bool = False,
) -> np.ndarray:
    """One Optuna trial: train XGBoost from LibSVM only; return validation positive scores."""
    model, _metrics = train_xgboost_from_libsvm_disk(
        bundle,
        params,
        y_val=y_val,
        backend_runtime_params=backend_runtime_params,
        val_dec026_window_hours=None,
        val_dec026_min_alerts_per_hour=None,
        use_external_memory=use_external_memory,
    )
    return np.asarray(model.predict_val_scores_from_libsvm(), dtype=np.float64).reshape(-1)


def hpo_trial_val_scores_catboost_from_libsvm(
    params: Mapping[str, Any],
    bundle: GbmBakeoffLibSvmBundle,
    y_val: Any,
    *,
    backend_runtime_params: Optional[Mapping[str, Any]] = None,
    quantize_first: bool = False,
) -> np.ndarray:
    """One Optuna trial: train CatBoost from LibSVM only; return validation positive scores."""
    from catboost import Pool

    model, _metrics = train_catboost_from_libsvm_disk(
        bundle,
        params,
        y_val=y_val,
        backend_runtime_params=backend_runtime_params,
        val_dec026_window_hours=None,
        val_dec026_min_alerts_per_hour=None,
        quantize_first=quantize_first,
    )
    vuri = getattr(model, "_gbm_bakeoff_valid_libsvm_uri", None)
    if not vuri:
        raise RuntimeError("hpo_trial_val_scores_catboost_from_libsvm: missing valid LibSVM URI on model")
    return np.asarray(model.predict_proba(Pool(str(vuri)))[:, 1], dtype=np.float64).reshape(-1)


def xgboost_disk_strict_refit_on_train_union_valid(
    model: Any,
    bundle: GbmBakeoffLibSvmBundle,
    hp: Mapping[str, Any],
    *,
    backend_runtime_params: Optional[Mapping[str, Any]] = None,
    use_external_memory: bool = False,
) -> Any:
    """File-backed final refit on train∪valid LibSVM (Issue #25), ``num_boost_round=best_iteration+1``."""
    import tempfile
    import xgboost as xgb

    from trainer.training.split_file_bundle import merge_libsvm_files, merge_train_valid_weight_files

    booster = getattr(model, "booster_", None) or getattr(model, "_Booster", None)
    if booster is None:
        raise RuntimeError("xgboost_disk_strict_refit_on_train_union_valid: model has no booster")
    best_it = int(getattr(booster, "best_iteration", -1))
    n_rounds = max(1, best_it + 1) if best_it >= 0 else max(1, int(hp.get("n_estimators", 100)))
    cache_dir = Path(bundle.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    fd, p_merged = tempfile.mkstemp(prefix=".a3_tv_xgb_", suffix=".libsvm", dir=str(cache_dir))
    os.close(fd)
    merged = Path(p_merged)
    merged_w_txt = Path(str(merged) + ".weight")
    try:
        n_merged = merge_libsvm_files(merged, [bundle.train_libsvm, bundle.valid_libsvm])
        merge_train_valid_weight_files(
            merged_w_txt,
            train_weight_txt=Path(str(bundle.train_libsvm) + ".weight"),
            valid_libsvm=bundle.valid_libsvm,
        )
        w_mm, _nr = ensure_train_weight_f32_memmap(merged, expected_rows=int(n_merged))
        device = str((backend_runtime_params or {}).get("device", "cpu")).lower()
        ext_ok = bool(use_external_memory) and not device.startswith("cuda")
        train_uri = xgboost_libsvm_uri(merged, external_memory=ext_ok, cache_dir=cache_dir)
        x_hp = dict(hp)
        if backend_runtime_params:
            x_hp.update(dict(backend_runtime_params))
        x_hp.pop("n_estimators", None)
        params = _xgb_hp_to_train_params(x_hp)
        dtrain = xgb.DMatrix(
            train_uri,
            weight=w_mm,
            feature_names=list(bundle.feature_names),
        )
        new_booster = xgb.train(params, dtrain, num_boost_round=int(n_rounds), verbose_eval=False)
        valid_uri = xgboost_libsvm_uri(
            bundle.valid_libsvm, external_memory=False, cache_dir=cache_dir
        )
        test_uri = (
            xgboost_libsvm_uri(bundle.test_libsvm, external_memory=False, cache_dir=cache_dir)
            if bundle.test_libsvm is not None
            else None
        )
        orig_train_uri = xgboost_libsvm_uri(
            bundle.train_libsvm,
            external_memory=ext_ok,
            cache_dir=cache_dir,
        )
        return XGBoostBoosterDiskClassifier(
            new_booster,
            feature_names=bundle.feature_names,
            valid_libsvm_uri=valid_uri,
            test_libsvm_uri=test_uri,
            train_libsvm_uri=orig_train_uri,
        )
    finally:
        try:
            merged.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            merged_w_txt.unlink(missing_ok=True)
        except OSError:
            pass
        Path(str(merged) + ".weight.f32").unlink(missing_ok=True)


def catboost_disk_strict_refit_on_train_union_valid(
    model: Any,
    bundle: GbmBakeoffLibSvmBundle,
    hp: Mapping[str, Any],
    *,
    backend_runtime_params: Optional[Mapping[str, Any]] = None,
    quantize_first: bool = False,
) -> Any:
    """File-backed final refit on train∪valid LibSVM for CatBoost (Issue #25)."""
    import tempfile
    from catboost import CatBoostClassifier, Pool

    from trainer.training.split_file_bundle import merge_libsvm_files, merge_train_valid_weight_files
    from trainer.training.trainer import _sanitize_catboost_params_for_runtime

    cache_dir = Path(bundle.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    fd, p_merged = tempfile.mkstemp(prefix=".a3_tv_cb_", suffix=".libsvm", dir=str(cache_dir))
    os.close(fd)
    merged = Path(p_merged)
    merged_w_txt = Path(str(merged) + ".weight")
    try:
        n_merged = merge_libsvm_files(merged, [bundle.train_libsvm, bundle.valid_libsvm])
        merge_train_valid_weight_files(
            merged_w_txt,
            train_weight_txt=Path(str(bundle.train_libsvm) + ".weight"),
            valid_libsvm=bundle.valid_libsvm,
        )
        w_mm, _nr = ensure_train_weight_f32_memmap(merged, expected_rows=int(n_merged))
        merged_1, _hit_m = catboost_libsvm_path_one_based_cached(merged, cache_dir)
        train_uri = catboost_libsvm_uri(merged_1)
        c_hp = dict(hp)
        if backend_runtime_params:
            c_hp.update(dict(backend_runtime_params))
        c_hp.pop("class_weights", None)
        c_hp.pop("early_stopping_rounds", None)
        base_iters = int(c_hp.pop("iterations"))
        try:
            bi = int(model.get_best_iteration())
        except Exception:
            bi = -1
        new_iters = max(1, bi + 1) if bi >= 0 else max(1, min(base_iters, int(n_merged)))
        c_hp = _sanitize_catboost_params_for_runtime(c_hp)
        train_pool = Pool(data=train_uri)
        train_pool.set_weight(w_mm)
        if quantize_first:
            try:
                from catboost import quantize as cb_quantize

                train_pool = cb_quantize(train_pool)
            except Exception as exc:
                raise RuntimeError(f"CatBoost quantize() failed on refit pool: {exc}") from exc
        refit_model = CatBoostClassifier(iterations=int(new_iters), **c_hp)
        refit_model.fit(train_pool, verbose=False)
        valid_1, _hv = catboost_libsvm_path_one_based_cached(bundle.valid_libsvm, cache_dir)
        valid_uri = catboost_libsvm_uri(valid_1)
        test_1: Optional[Path]
        if bundle.test_libsvm is not None:
            test_1, _ht = catboost_libsvm_path_one_based_cached(bundle.test_libsvm, cache_dir)
        else:
            test_1 = None
        train_1, _htr = catboost_libsvm_path_one_based_cached(bundle.train_libsvm, cache_dir)
        train_orig_uri = catboost_libsvm_uri(train_1)
        setattr(refit_model, "_gbm_bakeoff_valid_libsvm_uri", valid_uri)
        setattr(refit_model, "_gbm_bakeoff_train_libsvm_uri", train_orig_uri)
        setattr(
            refit_model,
            "_gbm_bakeoff_test_libsvm_uri",
            catboost_libsvm_uri(test_1) if test_1 is not None else None,
        )
        setattr(refit_model, "a3_final_fit_mode", "libsvm_disk")
        return refit_model
    finally:
        try:
            merged.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            merged_w_txt.unlink(missing_ok=True)
        except OSError:
            pass
        Path(str(merged) + ".weight.f32").unlink(missing_ok=True)
