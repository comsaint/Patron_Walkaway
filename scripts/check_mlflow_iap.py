#!/usr/bin/env python3
"""
Ping MLflow behind IAP using a service account JSON key (no key printed).

Prereqs:
  - credential/mlflow.env (or env) sets MLFLOW_TRACKING_URI and GOOGLE_APPLICATION_CREDENTIALS
  - MLFLOW_IAP_AUDIENCE = OAuth 2.0 Client ID used by IAP, e.g. 123456789.apps.googleusercontent.com
    (Cloud Run URL is NOT valid as audience when IAP is enabled.)

Run from repo root:
  python scripts/check_mlflow_iap.py
  python scripts/check_mlflow_iap.py --iap-audience YOUR_CLIENT_ID.apps.googleusercontent.com
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_key_path(root: Path, raw: str) -> Path:
    p = Path(raw.strip())
    if p.is_absolute():
        return p
    if raw.strip().startswith("./"):
        return (root / raw.strip()[2:]).resolve()
    return (root / p).resolve()


def main() -> int:
    root = _repo_root()
    env_path = root / "credential" / "mlflow.env"
    if env_path.is_file():
        load_dotenv(str(env_path), override=False)

    parser = argparse.ArgumentParser(description="GET MLflow /version with IAP ID token")
    parser.add_argument(
        "--iap-audience",
        default=os.environ.get("MLFLOW_IAP_AUDIENCE", "").strip(),
        help="IAP OAuth client ID (…apps.googleusercontent.com). Env: MLFLOW_IAP_AUDIENCE.",
    )
    args = parser.parse_args()

    base = (os.environ.get("MLFLOW_TRACKING_URI") or "").strip().rstrip("/")
    raw_key = (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if not base:
        print("MLFLOW_TRACKING_URI is not set (e.g. in credential/mlflow.env).", file=sys.stderr)
        return 2
    if not raw_key:
        print("GOOGLE_APPLICATION_CREDENTIALS is not set.", file=sys.stderr)
        return 2
    if not args.iap_audience:
        print(
            "Missing IAP audience. Set MLFLOW_IAP_AUDIENCE to the OAuth client ID from IAP "
            "(…apps.googleusercontent.com), not the Cloud Run URL.\n"
            "GCP Console: Security - Identity-Aware Proxy - your backend - OAuth client ID.",
            file=sys.stderr,
        )
        return 2

    key_path = _resolve_key_path(root, raw_key)
    if not key_path.is_file():
        print(f"Service account key not found: {key_path}", file=sys.stderr)
        return 2

    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(key_path)

    try:
        import google.auth.transport.requests  # type: ignore[import-untyped]
        import google.oauth2.id_token  # type: ignore[import-untyped]
    except ImportError as e:
        print("Install google-auth (see requirements.txt).", file=sys.stderr)
        print(e, file=sys.stderr)
        return 2

    request = google.auth.transport.requests.Request()
    try:
        token = google.oauth2.id_token.fetch_id_token(request, args.iap_audience)
    except Exception as e:
        print(f"Failed to mint ID token: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    url = f"{base}/version"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace").strip()
            print(f"OK HTTP {resp.status} {url}")
            print(body[:500] + ("…" if len(body) > 500 else ""))
            return 0
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace").strip()
        print(f"HTTP {e.code} {url}", file=sys.stderr)
        print(err_body[:500], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
