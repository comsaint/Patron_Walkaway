"""Resolve versioned Step-5 model bundles under an ``out/*/`` root directory."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

LATEST_MODEL_MANIFEST_NAME: Final[str] = "_latest_model_manifest.json"
TRAINING_METRICS_FILENAME: Final[str] = "training_metrics.json"
RUN_REPORT_FILENAME: Final[str] = "run_report.json"
SPLIT_REPORT_FILENAME: Final[str] = "split_report.json"
TRAINING_METRICS_SCHEMA: Final[str] = "training-metrics.hightier.v1"
RUN_REPORT_SCHEMA: Final[str] = "trainer_hightier.run_report.v1"
FEATURE_PARITY_REPORT_FILENAME: Final[str] = "feature_parity_verification.json"
DEPLOY_E2E_GATE_REPORT_FILENAME: Final[str] = "deploy_e2e_gate_report.json"
OFFLINE_SERVING_BACKTEST_REPORT_FILENAME: Final[str] = "offline_serving_backtest.json"
SUPPLIER_ROOT_CAUSE_REPORT_FILENAME: Final[str] = "supplier_root_cause.json"
SHORT_TERM_PARITY_REPORT_FILENAME: Final[str] = "short_term_parity_verification.json"
_MODEL_VERSION_SAFE_RE: Final[re.Pattern[str]] = re.compile(r"^[\w.\-]+$")


def model_bundle_report_path(bundle_dir: Path | str, filename: str) -> Path:
    """Return ``bundle_dir / filename`` for run-scoped JSON reports."""

    root = Path(bundle_dir).expanduser().resolve()
    name = str(filename).strip()
    if not name or "/" in name or "\\" in name:
        raise ValueError(f"Unsafe report filename {filename!r}")
    return root / name


def _embedded_model_dir_from_deploy(deploy_root: Path) -> Path:
    """Resolve embedded ``models/`` (or configured rel path) inside a deploy bundle."""

    rel_path = deploy_root / "deploy_bundle_paths.json"
    if rel_path.is_file():
        raw = json.loads(rel_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return deploy_root / str(raw.get("model_bundle_dir", "models"))
    return deploy_root / "models"


def resolve_model_bundle_for_reports(
    *,
    model_dir: Path | str | None = None,
    deploy_bundle_dir: Path | str | None = None,
    versions_root: Path | str | None = None,
) -> Path:
    """Locate the Step-5 bundle directory for run-scoped report output.

    Preference order:

    #. Explicit ``model_dir`` when it contains ``model.pkl``.
    #. ``versions_root / model_version`` when deploy bundle embeds ``model_version``
       and the canonical training bundle still exists on disk.
    #. Embedded deploy ``models/`` directory when it contains ``model.pkl``.
    """

    if model_dir is not None:
        explicit = Path(model_dir).expanduser().resolve()
        if not (explicit / "model.pkl").is_file():
            raise FileNotFoundError(f"No model.pkl under model bundle {explicit}")
        return explicit

    if deploy_bundle_dir is None:
        raise ValueError("provide model_dir or deploy_bundle_dir")

    deploy_root = Path(deploy_bundle_dir).expanduser().resolve()
    embedded = _embedded_model_dir_from_deploy(deploy_root).resolve()
    ver_path = embedded / "model_version"
    if ver_path.is_file():
        from trainer_hightier.config import DEFAULT_MODEL_DIR

        mv = ver_path.read_text(encoding="utf-8").strip()
        root = Path(versions_root or DEFAULT_MODEL_DIR).expanduser().resolve()
        if mv:
            canonical = (root / mv).resolve()
            if (canonical / "model.pkl").is_file():
                return canonical

    if (embedded / "model.pkl").is_file():
        return embedded

    raise FileNotFoundError(
        "Could not resolve model bundle for reports: "
        f"deploy={deploy_root}, embedded={embedded}",
    )


def safe_version_subdirectory(versions_root: Path | str, model_version: str) -> Path:
    """Return ``versions_root / model_version`` after validating *model_version* is a single safe segment."""

    root = Path(versions_root).expanduser().resolve()
    mv = str(model_version).strip()
    if not mv or "/" in mv or "\\" in mv or not _MODEL_VERSION_SAFE_RE.fullmatch(mv):
        raise ValueError(f"Unsafe or empty model_version {model_version!r}")
    return root / mv


def write_latest_model_manifest(
    versions_root: Path | str,
    model_version: str,
    bundle_dir: Path | str,
) -> None:
    """Write ``bundle_relative`` pointer for :func:`resolve_model_bundle_dir`."""

    root = Path(versions_root).expanduser().resolve()
    bd = Path(bundle_dir).expanduser().resolve()
    expected = (root / str(model_version).strip()).resolve()
    if bd != expected:
        rel_any = bd.relative_to(root).as_posix()
        blob: dict[str, Any] = {"bundle_relative": rel_any}
    else:
        blob = {"bundle_relative": str(model_version).strip()}
    out = root / LATEST_MODEL_MANIFEST_NAME
    out.write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")


def resolve_model_bundle_dir(
    versions_root: Path | str,
    *,
    explicit_dir: Path | str | None = None,
    model_version: str | None = None,
) -> Path:
    """Locate a bundle directory containing ``model.pkl``.

    Preference order mirrors legacy trainer CLI:

    #. Explicit directory
    #. Named ``model_version`` child of *versions_root*
    #. ``_latest_model_manifest.json``
    #. Legacy flat bundle at *versions_root*

    Raises
    ------
    FileNotFoundError
        When ``model.pkl`` cannot be resolved.
    """

    root = Path(versions_root).expanduser().resolve()

    if explicit_dir is not None:
        d = Path(explicit_dir).expanduser().resolve()
        if not (d / "model.pkl").is_file():
            raise FileNotFoundError(f"No model.pkl under explicit bundle dir {d}")
        return d

    mv = str(model_version or "").strip()
    if mv:
        cand = root / mv
        if not (cand / "model.pkl").is_file():
            raise FileNotFoundError(f"No model.pkl under versions_root/{mv}: {cand}")
        return cand.resolve()

    manifest = root / LATEST_MODEL_MANIFEST_NAME
    if manifest.is_file():
        raw_obj = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(raw_obj, dict):
            raise ValueError(f"{manifest}: root must be a JSON object, got {type(raw_obj)!r}")
        rel = raw_obj.get("bundle_relative")
        if not rel or not str(rel).strip():
            raise ValueError(f"{manifest}: missing bundle_relative")
        cand = Path(root, *Path(str(rel)).parts).resolve()
        try:
            cand.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{manifest}: bundle_relative resolves outside versions root ({cand})") from exc
        if not (cand / "model.pkl").is_file():
            raise FileNotFoundError(f"Latest manifest points to {cand}, but model.pkl is missing.")
        return cand

    legacy = root / "model.pkl"
    if legacy.is_file():
        return root.resolve()

    raise FileNotFoundError(
        "Could not resolve model bundle: missing _latest_model_manifest.json, "
        f"explicit model_version, and flat model.pkl under {root}"
    )
