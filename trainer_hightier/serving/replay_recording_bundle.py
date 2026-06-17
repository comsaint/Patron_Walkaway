"""Offline replay and analysis entrypoint for flight recording bundles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_recording_manifest(recording_root: Path) -> dict[str, Any]:
    """Load and return ``MANIFEST.json`` from *recording_root*."""
    path = recording_root / "MANIFEST.json"
    if not path.is_file():
        raise FileNotFoundError(f"MANIFEST.json not found under {recording_root}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest_files(recording_root: Path, manifest: dict[str, Any]) -> list[str]:
    """Return list of manifest paths missing or with sha256 mismatch."""
    errors: list[str] = []
    from trainer_hightier.serving.flight_recorder.manifest import sha256_file

    for entry in manifest.get("files", []):
        rel = str(entry.get("path", ""))
        expected = str(entry.get("sha256", ""))
        file_path = recording_root / rel
        if not file_path.is_file():
            errors.append(f"missing: {rel}")
            continue
        actual = sha256_file(file_path)
        if expected and actual != expected:
            errors.append(f"sha256 mismatch: {rel}")
    return errors


def run_full_analysis(
    recording_root: Path,
    output_dir: Path,
    *,
    model_bundle_dir: Path | None = None,
) -> dict[str, Any]:
    """Run replay, casebooks, and CH diff aggregation into *output_dir*."""
    from trainer_hightier.serving.flight_recorder.casebook import write_casebooks
    from trainer_hightier.serving.flight_recorder.feature_root_cause import (
        write_feature_root_cause_report,
    )
    from trainer_hightier.serving.flight_recorder.late_arrival_report import (
        write_late_arrival_reports,
    )
    from trainer_hightier.serving.flight_recorder.replay_score import (
        run_score_replay,
        write_score_replay_report,
    )
    from trainer_hightier.serving.flight_recorder.replay_validator import (
        run_validator_replay,
        write_validator_replay_report,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    summary = build_bundle_summary(recording_root)
    reports: dict[str, Any] = {"bundle_load_summary": summary}
    if model_bundle_dir is not None and model_bundle_dir.is_dir():
        score_report = run_score_replay(recording_root, model_bundle_dir)
        reports["score_replay"] = score_report
        write_score_replay_report(output_dir, score_report)
    val_report = run_validator_replay(recording_root)
    reports["validator_replay"] = val_report
    write_validator_replay_report(output_dir, val_report)
    reports["casebooks"] = write_casebooks(output_dir, recording_root)
    reports["late_arrival"] = {
        str(k): str(v) for k, v in write_late_arrival_reports(output_dir, recording_root).items()
    }
    reports["feature_root_cause"] = str(
        write_feature_root_cause_report(output_dir, recording_root)
    )
    master = output_dir / "analysis_summary.json"
    master.write_text(json.dumps(reports, indent=2, default=str), encoding="utf-8")
    reports["analysis_summary_path"] = str(master)
    return reports


def build_bundle_summary(recording_root: Path) -> dict[str, Any]:
    """Summarize recording bundle layout for offline inspection."""
    manifest = load_recording_manifest(recording_root)
    errors = validate_manifest_files(recording_root, manifest)
    scorer_cycles = sorted((recording_root / "cycles" / "scorer").glob("cycle_*"))
    validator_cycles = sorted((recording_root / "cycles" / "validator").glob("cycle_*"))
    ch_windows = sorted((recording_root / "ch_time_machine").glob("window_*"))
    return {
        "recording_root": str(recording_root),
        "schema_version": manifest.get("schema_version"),
        "model_version": manifest.get("model_version"),
        "partial": manifest.get("partial"),
        "manifest_file_errors": errors,
        "n_scorer_cycles": len(scorer_cycles),
        "n_validator_cycles": len(validator_cycles),
        "n_ch_time_machine_windows": len(ch_windows),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI: validate bundle and write summary JSON."""
    parser = argparse.ArgumentParser(description="Replay / analyze flight recording bundle")
    parser.add_argument("--recording-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--model-bundle-dir",
        type=Path,
        default=None,
        help="Deploy model dir with model.pkl (required for score replay).",
    )
    parser.add_argument(
        "--full-analysis",
        action="store_true",
        help="Run score/validator replay, casebooks, and CH diff reports.",
    )
    args = parser.parse_args(argv)
    recording_root = args.recording_root.resolve()
    output_dir = args.output_dir.resolve()
    if args.full_analysis:
        reports = run_full_analysis(
            recording_root,
            output_dir,
            model_bundle_dir=args.model_bundle_dir,
        )
        print(json.dumps(reports.get("bundle_load_summary", reports), indent=2))
        return 0
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = build_bundle_summary(recording_root)
    out_path = output_dir / "bundle_load_summary.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if not summary.get("manifest_file_errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
