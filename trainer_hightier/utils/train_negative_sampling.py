"""Train-only negative downsampling between Step 4 splits and Step 5 fit (SSOT TA-008)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trainer_hightier.config import SamplePolicy, sample_policy_fingerprint
from trainer_hightier.utils.assembly_cache_v1 import parquet_content_fingerprint

logger = logging.getLogger(__name__)

LABEL_COLUMN: Final[str] = "walkaway_label"
SAMPLED_TRAIN_PARQUET_NAME: Final[str] = "train_sampled.parquet"
SAMPLED_TRAIN_MANIFEST_NAME: Final[str] = "train_sampled.manifest.json"
SAMPLED_TRAIN_CACHE_KIND: Final[str] = "sampled_train_v1"


def downsample_train_negatives(
    df: pd.DataFrame,
    *,
    neg_sample_frac: float,
    neg_sample_seed: int,
    label_column: str = LABEL_COLUMN,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Keep all positives; randomly subsample negatives with a fixed seed."""
    if label_column not in df.columns:
        raise ValueError(
            f"train parquet missing label column {label_column!r}; "
            f"columns={list(df.columns)}",
        )
    frac = float(neg_sample_frac)
    if not (0.0 < frac <= 1.0):
        raise ValueError(f"neg_sample_frac must be in (0, 1], got {neg_sample_frac!r}")
    labels = pd.to_numeric(df[label_column], errors="coerce")
    null_ratio = float(labels.isna().mean())
    if null_ratio > 0.0:
        raise ValueError(
            f"label column {label_column!r} has null_ratio={null_ratio:.6f}; expected 0",
        )
    pos_mask = labels >= 0.5
    neg_mask = ~pos_mask
    pos_df = df.loc[pos_mask]
    neg_df = df.loc[neg_mask]
    n_pos = int(len(pos_df))
    n_neg = int(len(neg_df))
    if frac >= 1.0 or n_neg == 0:
        counts = {
            "train_rows_before": int(len(df)),
            "train_rows_after": int(len(df)),
            "train_positives_kept": n_pos,
            "train_negatives_before": n_neg,
            "train_negatives_after": n_neg,
        }
        return df, counts
    n_keep = int(round(n_neg * frac))
    n_keep = max(0, min(n_neg, n_keep))
    rng = np.random.default_rng(int(neg_sample_seed))
    if n_keep <= 0:
        neg_kept = neg_df.iloc[0:0]
    elif n_keep == n_neg:
        neg_kept = neg_df
    else:
        keep_idx = rng.choice(neg_df.index.to_numpy(), size=n_keep, replace=False)
        neg_kept = neg_df.loc[keep_idx]
    out = pd.concat([pos_df, neg_kept], axis=0).sort_index()
    counts = {
        "train_rows_before": int(len(df)),
        "train_rows_after": int(len(out)),
        "train_positives_kept": n_pos,
        "train_negatives_before": n_neg,
        "train_negatives_after": int(len(neg_kept)),
    }
    return out, counts


def _manifest_path(splits_dir: Path) -> Path:
    return Path(splits_dir).resolve() / SAMPLED_TRAIN_MANIFEST_NAME


def _sampled_train_path(splits_dir: Path) -> Path:
    return Path(splits_dir).resolve() / SAMPLED_TRAIN_PARQUET_NAME


def load_sampled_train_manifest(splits_dir: Path) -> dict[str, Any] | None:
    """Load sampled-train sidecar manifest when present."""
    path = _manifest_path(splits_dir)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"{path} must contain a JSON object, got {type(payload).__name__}")
    return payload


def sampled_train_cache_hit(
    manifest: dict[str, Any] | None,
    *,
    train_source_fingerprint_sha256_hex: str,
    policy_fingerprint: str,
) -> bool:
    """Return True when manifest matches train source + sample policy fingerprints."""
    if manifest is None:
        return False
    expected = sampled_train_cache_identity(
        train_source_fingerprint_sha256_hex=train_source_fingerprint_sha256_hex,
        sample_policy_fingerprint=policy_fingerprint,
    )
    return all(str(manifest.get(key)) == str(value) for key, value in expected.items())


def sampled_train_cache_identity(
    *,
    train_source_fingerprint_sha256_hex: str,
    sample_policy_fingerprint: str,
) -> dict[str, str]:
    """Stable cache identity for sampled train artifacts (SSOT §6.2)."""
    src = str(train_source_fingerprint_sha256_hex).strip()
    pol = str(sample_policy_fingerprint).strip()
    if not src:
        raise ValueError("train_source_fingerprint_sha256_hex must be non-empty")
    if not pol:
        raise ValueError("sample_policy_fingerprint must be non-empty")
    return {
        "kind": SAMPLED_TRAIN_CACHE_KIND,
        "train_source_fingerprint_sha256_hex": src,
        "sample_policy_fingerprint": pol,
    }


def write_sampled_train_manifest(
    splits_dir: Path,
    *,
    train_source_fingerprint_sha256_hex: str,
    policy: SamplePolicy,
    counts: dict[str, int],
    sampled_train_parquet: Path,
) -> Path:
    """Persist sampled-train cache manifest beside Step 4 splits."""
    policy_fp = sample_policy_fingerprint(policy)
    payload: dict[str, Any] = {
        **sampled_train_cache_identity(
            train_source_fingerprint_sha256_hex=train_source_fingerprint_sha256_hex,
            sample_policy_fingerprint=policy_fp,
        ),
        "neg_sample_frac": float(policy.neg_sample_frac),
        "neg_sample_seed": int(policy.neg_sample_seed),
        "neg_sample_scope": str(policy.neg_sample_scope),
        "sampled_train_parquet": str(Path(sampled_train_parquet).resolve()),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        **counts,
    }
    path = _manifest_path(splits_dir)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def materialize_sampled_train_parquet(
    *,
    train_parquet: Path,
    splits_dir: Path,
    policy: SamplePolicy,
    force_refresh: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Build or reuse ``train_sampled.parquet`` for Step 5 when downsampling is enabled."""
    src = Path(train_parquet).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Step 4 train split missing at {src}")
    sd = Path(splits_dir).resolve()
    sd.mkdir(parents=True, exist_ok=True)
    policy_fp = sample_policy_fingerprint(policy)
    meta_base: dict[str, Any] = {
        **policy.manifest_block(),
        "sample_policy_fingerprint": policy_fp,
        "val_test_evaluation_unsampled": True,
        "train_parquet_source": str(src),
    }
    if float(policy.neg_sample_frac) >= 1.0:
        meta = {
            **meta_base,
            "enabled": False,
            "cache_hit": True,
            "sampled_train_parquet": str(src),
            "train_rows_before": None,
            "train_rows_after": None,
        }
        return src, meta

    src_fp = parquet_content_fingerprint(src)
    if src_fp is None:
        raise ValueError(f"cannot fingerprint train parquet at {src}")
    out_p = _sampled_train_path(sd)
    manifest = load_sampled_train_manifest(sd)
    if (
        not force_refresh
        and out_p.is_file()
        and sampled_train_cache_hit(
            manifest,
            train_source_fingerprint_sha256_hex=src_fp,
            policy_fingerprint=policy_fp,
        )
    ):
        logger.info(
            "[sample_policy] cache hit sampled train -> %s (frac=%.4f seed=%d)",
            out_p.name,
            float(policy.neg_sample_frac),
            int(policy.neg_sample_seed),
        )
        counts = {
            key: int(manifest[key])
            for key in (
                "train_rows_before",
                "train_rows_after",
                "train_positives_kept",
                "train_negatives_before",
                "train_negatives_after",
            )
            if isinstance(manifest, dict) and manifest.get(key) is not None
        }
        return out_p, {
            **meta_base,
            "enabled": True,
            "cache_hit": True,
            "sampled_train_parquet": str(out_p),
            **counts,
        }

    df = pd.read_parquet(src)
    sampled, counts = downsample_train_negatives(
        df,
        neg_sample_frac=float(policy.neg_sample_frac),
        neg_sample_seed=int(policy.neg_sample_seed),
    )
    pq.write_table(pa.Table.from_pandas(sampled, preserve_index=True), out_p)
    write_sampled_train_manifest(
        sd,
        train_source_fingerprint_sha256_hex=src_fp,
        policy=policy,
        counts=counts,
        sampled_train_parquet=out_p,
    )
    logger.info(
        "[sample_policy] materialized sampled train rows %d -> %d (neg frac=%.4f) -> %s",
        counts["train_rows_before"],
        counts["train_rows_after"],
        float(policy.neg_sample_frac),
        out_p.name,
    )
    return out_p, {
        **meta_base,
        "enabled": True,
        "cache_hit": False,
        "sampled_train_parquet": str(out_p),
        **counts,
    }
