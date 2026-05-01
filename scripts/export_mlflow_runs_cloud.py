#!/usr/bin/env python3
"""
Export MLflow runs from a remote HTTPS tracking server using Bearer auth.

Authentication (pick one):
  - Service account JSON + IAP: set --iap-audience OR MLFLOW_IAP_AUDIENCE to the
    IAP "OAuth 2.0 Client ID" (ends with .apps.googleusercontent.com). Do NOT use
    the Cloud Run URL as audience (IAP will return 401 Invalid JWT audience).
  - Service account JSON + Cloud Run (IAM only, no IAP): omit IAP audience; uses
    --identity-audience if set, else the tracking URI origin as target_audience.
  - Pre-minted token: pass --bearer-token (or --bearer-token-file); then
    --credentials is not required.

Writes per run: <output-dir>/<run_id>/run.json and optional artifacts/ subtree.
GCS-backed artifacts require package google-cloud-storage (google.cloud.storage).

Example:
  python scripts/export_mlflow_runs_cloud.py \\
    --credentials credential/sa.json \\
    --tracking-uri https://mlflow-server-xxxxx.us-central1.run.app \\
    --run-id abc123 --run-id def456 \\
    --output-dir .tmp/mlflow_export \\
    --iap-audience 123456789-xxxxx.apps.googleusercontent.com
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient


def _repo_root() -> Path:
    """Return repository root (parent of scripts/)."""
    return Path(__file__).resolve().parent.parent


def _resolve_path(root: Path, raw: str) -> Path:
    """Resolve raw path relative to repo root when not absolute."""
    p = Path(raw.strip())
    if p.is_absolute():
        return p
    if raw.strip().startswith("./"):
        return (root / raw.strip()[2:]).resolve()
    return (root / p).resolve()


def _load_mlflow_env(root: Path) -> None:
    """Load credential/mlflow.env if present (same pattern as check_mlflow_iap.py)."""
    env_path = root / "credential" / "mlflow.env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv  # type: ignore[import-untyped]
    except ImportError:
        return
    load_dotenv(str(env_path), override=False)


def _iap_audience_error_message(*, used_iap_mint: bool) -> str:
    """Return a short hint when IAP rejects the token audience."""
    base = (
        "IAP rejected the bearer token (wrong JWT aud claim).\n"
        "- Find the OAuth 2.0 Client ID for this IAP-protected resource in GCP Console:\n"
        "  Security > Identity-Aware Proxy > (your backend) > OAuth client ID\n"
        "  It looks like NNN.apps.googleusercontent.com (NOT your Cloud Run URL).\n"
        "- Pass: --iap-audience THAT_CLIENT_ID\n"
        "  Or set MLFLOW_IAP_AUDIENCE in credential/mlflow.env\n"
        "- Verify with: python scripts/check_mlflow_iap.py --iap-audience THAT_CLIENT_ID\n"
    )
    if used_iap_mint:
        return base + (
            "You already used IAP minting; if this still fails, the client ID does not "
            "match the IAP backend for this MLflow URL.\n"
        )
    return base + (
        "You minted a Cloud Run IAM token (no --iap-audience). If this service sits "
        "behind IAP, you must use IAP audience above instead.\n"
    )


def _read_bearer_token(path: Path) -> str:
    """Read bearer token from a single-line or JSON file with access_token key."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Empty token file: {path}")
    if text.startswith("{"):
        data = json.loads(text)
        if "access_token" in data:
            return str(data["access_token"]).strip()
        if "token" in data:
            return str(data["token"]).strip()
        raise ValueError("JSON token file must contain 'access_token' or 'token'")
    return text


def _mint_token_iap(credentials_path: Path, iap_audience: str) -> str:
    """Mint a Google ID token for IAP (audience = OAuth client ID)."""
    import google.auth.transport.requests  # type: ignore[import-untyped]
    import google.oauth2.id_token  # type: ignore[import-untyped]

    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(credentials_path)
    request = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(request, iap_audience)


def _mint_token_cloud_run(credentials_path: Path, audience: str) -> str:
    """Mint a Google ID token for Cloud Run (target_audience = service URL)."""
    from google.auth.transport.requests import Request  # type: ignore[import-untyped]
    from google.oauth2 import service_account  # type: ignore[import-untyped]

    creds = service_account.IDTokenCredentials.from_service_account_file(
        str(credentials_path),
        target_audience=audience.rstrip("/"),
    )
    creds.refresh(Request())
    if not creds.token:
        raise RuntimeError("IDTokenCredentials.refresh() produced no token")
    return str(creds.token)


def _run_info_to_dict(run: Any) -> dict[str, Any]:
    """Serialize mlflow.entities.RunInfo to a JSON-friendly dict."""
    info = run.info
    return {
        "run_id": info.run_id,
        "experiment_id": info.experiment_id,
        "status": info.status,
        "artifact_uri": info.artifact_uri,
        "lifecycle_stage": info.lifecycle_stage,
        "user_id": getattr(info, "user_id", None),
        "start_time": info.start_time,
        "end_time": info.end_time,
    }


def _export_one_run(client: Any, run_id: str, dest: Path, *, download_artifacts: bool) -> None:
    """Write run.json and optionally download artifacts for a single run_id."""
    run = client.get_run(run_id)
    metrics_hist: dict[str, list[dict[str, Any]]] = {}
    for key in sorted(run.data.metrics.keys()):
        hist = client.get_metric_history(run_id, key)
        metrics_hist[key] = [
            {"step": m.step, "value": m.value, "timestamp": m.timestamp} for m in hist
        ]
    payload: dict[str, Any] = {
        "info": _run_info_to_dict(run),
        "params": dict(run.data.params),
        "tags": dict(run.data.tags),
        "metrics": metrics_hist,
    }
    dest.mkdir(parents=True, exist_ok=True)
    run_json = dest / "run.json"
    run_json.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if not download_artifacts:
        return
    art_dir = dest / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    try:
        client.download_artifacts(run_id, "", str(art_dir))
    except Exception as exc:
        msg = str(exc)
        print(f"Warning: artifact download failed for {run_id}: {msg}", file=sys.stderr)
        if "google.cloud" in msg or "No module named 'google" in msg:
            print(
                "  Hint: artifact_uri is likely gs:// — pip install google-cloud-storage "
                "(see requirements.txt). run.json is still valid.",
                file=sys.stderr,
            )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse CLI arguments."""
    p = argparse.ArgumentParser(
        description="Export MLflow runs (JSON + optional artifacts) from HTTPS tracking.",
    )
    p.add_argument(
        "--credentials",
        default="",
        help="Path to GCP service account JSON key (not needed if --bearer-token).",
    )
    p.add_argument(
        "--tracking-uri",
        required=True,
        help="MLflow tracking base URL, e.g. https://xxx.run.app (no /api/...).",
    )
    p.add_argument(
        "--run-id",
        action="append",
        dest="run_ids",
        default=[],
        metavar="RUN_ID",
        help="Run UUID to export (repeatable).",
    )
    p.add_argument(
        "--output-dir",
        required=True,
        help="Directory under which each run is written to <run_id>/.",
    )
    p.add_argument(
        "--iap-audience",
        default="",
        help=(
            "IAP OAuth client ID (*.apps.googleusercontent.com), not the Cloud Run URL. "
            "If omitted, MLFLOW_IAP_AUDIENCE from env (e.g. credential/mlflow.env) is used."
        ),
    )
    p.add_argument(
        "--identity-audience",
        default="",
        help="For Cloud Run IAM only: ID token audience (default: same as --tracking-uri).",
    )
    p.add_argument(
        "--bearer-token",
        default="",
        help="Use this token as MLFLOW_TRACKING_TOKEN (skip service account mint).",
    )
    p.add_argument(
        "--bearer-token-file",
        default="",
        help="Read bearer token from file (single line or JSON with access_token).",
    )
    p.add_argument(
        "--skip-artifacts",
        action="store_true",
        help="Only write run.json (no artifact download; saves time and bandwidth).",
    )
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry: mint token if needed, then export each run."""
    root = _repo_root()
    _load_mlflow_env(root)
    args = _parse_args(argv)
    tracking_uri = args.tracking_uri.strip().rstrip("/")
    if not args.run_ids:
        print("Provide at least one --run-id.", file=sys.stderr)
        return 2

    bearer = (args.bearer_token or "").strip()
    if args.bearer_token_file:
        token_path = _resolve_path(root, args.bearer_token_file)
        bearer = _read_bearer_token(token_path)

    used_iap_mint = False
    if bearer:
        token = bearer
    else:
        if not args.credentials:
            print("Either --credentials (SA JSON) or --bearer-token/--bearer-token-file.", file=sys.stderr)
            return 2
        key_path = _resolve_path(root, args.credentials)
        if not key_path.is_file():
            print(f"Credentials file not found: {key_path}", file=sys.stderr)
            return 2
        iap = (args.iap_audience or os.environ.get("MLFLOW_IAP_AUDIENCE", "")).strip()
        if iap:
            token = _mint_token_iap(key_path, iap)
            used_iap_mint = True
        else:
            aud = (args.identity_audience or "").strip() or tracking_uri
            token = _mint_token_cloud_run(key_path, aud)

    os.environ["MLFLOW_TRACKING_URI"] = tracking_uri
    os.environ["MLFLOW_TRACKING_TOKEN"] = token

    client = MlflowClient(tracking_uri=tracking_uri)
    out_root = _resolve_path(root, args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    download_artifacts = not args.skip_artifacts

    for run_id in args.run_ids:
        rid = run_id.strip()
        if not rid:
            continue
        dest = out_root / rid
        print(f"Exporting run {rid} -> {dest}")
        try:
            _export_one_run(client, rid, dest, download_artifacts=download_artifacts)
        except MlflowException as exc:
            err = str(exc)
            if "Invalid JWT audience" in err or "Invalid IAP credentials" in err:
                print(_iap_audience_error_message(used_iap_mint=used_iap_mint), file=sys.stderr)
            raise
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
