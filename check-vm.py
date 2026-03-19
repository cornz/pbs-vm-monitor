#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_PBS_BASE_URL = "https://127.0.0.1:8007"
DEFAULT_PBS_DATASTORE = "offsite"
DEFAULT_MAX_BACKUP_AGE_HOURS = 24.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_SERVER_HOST = "0.0.0.0"
DEFAULT_SERVER_PORT = 8081
DEFAULT_VERIFY_TLS = True
ENV_FILE_NAME = ".env"


@dataclass(frozen=True)
class Config:
    pbs_base_url: str
    pbs_datastore: str
    pbs_authorization: str
    max_backup_age_hours: float
    request_timeout_seconds: int
    server_host: str
    server_port: int
    verify_tls: bool

    @property
    def snapshots_url(self) -> str:
        base = self.pbs_base_url.rstrip("/")
        datastore = quote(self.pbs_datastore, safe="")
        return f"{base}/api2/json/admin/datastore/{datastore}/snapshots"


def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()

        key, sep, value = line.partition("=")
        if not sep:
            continue

        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        os.environ.setdefault(key, value)


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def env_or_default(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value is not None and value.strip() else default


def required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"Missing required environment variable: {name}")
    return value.strip()


def build_pbs_authorization() -> str:
    raw_authorization = os.getenv("PBS_AUTHORIZATION")
    if raw_authorization and raw_authorization.strip():
        authorization = raw_authorization.strip()
        if authorization.startswith("PBSAPIToken="):
            return authorization
        return f"PBSAPIToken={authorization}"

    token_id = os.getenv("PBS_TOKEN_ID")
    token_secret = os.getenv("PBS_TOKEN_SECRET")
    if token_id and token_id.strip() and token_secret and token_secret.strip():
        return f"PBSAPIToken={token_id.strip()}:{token_secret.strip()}"

    api_token = required_env("PBS_API_TOKEN")
    if api_token.startswith("PBSAPIToken="):
        return api_token
    if ":" not in api_token:
        raise ValueError(
            "PBS_API_TOKEN must look like 'user@realm!token-name:secret', "
            "or set PBS_TOKEN_ID and PBS_TOKEN_SECRET"
        )
    return f"PBSAPIToken={api_token}"


def load_config() -> Config:
    default_env_file = str(Path(__file__).with_name(ENV_FILE_NAME))
    env_file = Path(os.getenv("PBS_VM_MONITOR_ENV_FILE", default_env_file))
    load_dotenv(env_file)

    try:
        verify_tls = parse_bool(env_or_default("PBS_VERIFY_TLS", str(DEFAULT_VERIFY_TLS).lower()))
        max_backup_age_hours = float(
            env_or_default("MAX_BACKUP_AGE_HOURS", str(DEFAULT_MAX_BACKUP_AGE_HOURS))
        )
        request_timeout_seconds = int(
            env_or_default("REQUEST_TIMEOUT_SECONDS", str(DEFAULT_REQUEST_TIMEOUT_SECONDS))
        )
        server_port = int(env_or_default("SERVER_PORT", str(DEFAULT_SERVER_PORT)))
    except ValueError as exc:
        raise ValueError(f"Invalid configuration value: {exc}") from exc

    if max_backup_age_hours <= 0:
        raise ValueError("MAX_BACKUP_AGE_HOURS must be greater than 0")
    if request_timeout_seconds <= 0:
        raise ValueError("REQUEST_TIMEOUT_SECONDS must be greater than 0")
    if server_port <= 0 or server_port > 65535:
        raise ValueError("SERVER_PORT must be between 1 and 65535")

    return Config(
        pbs_base_url=env_or_default("PBS_BASE_URL", DEFAULT_PBS_BASE_URL),
        pbs_datastore=env_or_default("PBS_DATASTORE", DEFAULT_PBS_DATASTORE),
        pbs_authorization=build_pbs_authorization(),
        max_backup_age_hours=max_backup_age_hours,
        request_timeout_seconds=request_timeout_seconds,
        server_host=env_or_default("SERVER_HOST", DEFAULT_SERVER_HOST),
        server_port=server_port,
        verify_tls=verify_tls,
    )


def create_ssl_context(verify_tls: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def fetch_snapshots(config: Config) -> list[dict[str, Any]]:
    request = Request(
        config.snapshots_url,
        headers={
            "Accept": "application/json",
            "Authorization": config.pbs_authorization,
        },
    )

    try:
        with urlopen(
            request,
            timeout=config.request_timeout_seconds,
            context=create_ssl_context(config.verify_tls),
        ) as response:
            payload = json.load(response)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        details = f": {body}" if body else ""
        raise RuntimeError(f"PBS API returned HTTP {exc.code}{details}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach PBS API: {exc.reason}") from exc

    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError("PBS API response did not include a snapshot list in data")

    snapshots = [item for item in data if isinstance(item, dict)]
    if not snapshots:
        raise RuntimeError("PBS API returned no snapshots")

    return snapshots


def snapshot_timestamp(snapshot: dict[str, Any]) -> int | None:
    for key in ("backup-time", "backup_time", "time", "timestamp", "ctime"):
        value = snapshot.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def snapshot_label(snapshot: dict[str, Any]) -> str:
    backup_type = snapshot.get("backup-type") or snapshot.get("backup_type")
    backup_id = snapshot.get("backup-id") or snapshot.get("backup_id")
    backup_time = snapshot_timestamp(snapshot)

    if backup_type and backup_id and backup_time is not None:
        return f"{backup_type}/{backup_id}@{backup_time}"
    if backup_type and backup_id:
        return f"{backup_type}/{backup_id}"
    if backup_id:
        return str(backup_id)
    if snapshot.get("backup-dir"):
        return str(snapshot["backup-dir"])
    return "latest snapshot"


def snapshot_key(snapshot: dict[str, Any]) -> str:
    backup_type = snapshot.get("backup-type") or snapshot.get("backup_type")
    backup_id = snapshot.get("backup-id") or snapshot.get("backup_id")
    if backup_type and backup_id:
        return f"{backup_type}/{backup_id}"
    if backup_id:
        return str(backup_id)
    if snapshot.get("backup-dir"):
        return str(snapshot["backup-dir"])
    return "unknown"


def latest_snapshots_by_target(snapshots: list[dict[str, Any]]) -> dict[str, tuple[dict[str, Any], int]]:
    latest_by_target: dict[str, tuple[dict[str, Any], int]] = {}
    for snapshot in snapshots:
        timestamp = snapshot_timestamp(snapshot)
        if timestamp is None:
            continue

        key = snapshot_key(snapshot)
        current = latest_by_target.get(key)
        if current is None or timestamp > current[1]:
            latest_by_target[key] = (snapshot, timestamp)

    if not latest_by_target:
        raise RuntimeError("Could not find a usable timestamp in any PBS snapshot")

    return latest_by_target


def format_timestamp(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def run_check(config: Config) -> tuple[int, str]:
    try:
        snapshots = fetch_snapshots(config)
        latest_by_target = latest_snapshots_by_target(snapshots)
    except Exception as exc:
        return 1, f"CRITICAL: {exc}\n"

    now = time.time()
    lines: list[str] = []
    stale_count = 0

    for key in sorted(latest_by_target):
        snapshot, timestamp = latest_by_target[key]
        age_hours = max(0.0, (now - timestamp) / 3600)
        label = snapshot_label(snapshot)
        snapshot_time = format_timestamp(timestamp)
        if age_hours > config.max_backup_age_hours:
            stale_count += 1
            status = "CRITICAL"
        else:
            status = "OK"

        lines.append(
            f"{status}: {label} from {snapshot_time} is {age_hours:.2f} hours old "
            f"(limit {config.max_backup_age_hours:.2f}h)"
        )

    total = len(latest_by_target)
    if stale_count:
        summary = f"CRITICAL: {stale_count}/{total} backup targets exceed age limit"
        return 1, "\n".join([summary, *lines]) + "\n"

    summary = f"OK: all {total} backup targets are within age limit"
    return 0, "\n".join([summary, *lines]) + "\n"


class MonitorHandler(BaseHTTPRequestHandler):
    config: Config

    def do_GET(self) -> None:
        if self.path not in {"/", "/health"}:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not found\n")
            return

        status_code, body = run_check(self.config)
        self.send_response(200 if status_code == 0 else 500)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format_string: str, *args: Any) -> None:
        sys.stderr.write(f"{self.address_string()} - {format_string % args}\n")


def serve(config: Config) -> None:
    handler = type("ConfiguredMonitorHandler", (MonitorHandler,), {"config": config})
    server = HTTPServer((config.server_host, config.server_port), handler)
    print(f"Serving on http://{config.server_host}:{config.server_port}", flush=True)
    server.serve_forever()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Expose PBS backup freshness as an HTTP endpoint.")
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=("serve", "check"),
        help="Run a one-off health check or start the HTTP server (default: serve).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.command == "check":
        status_code, body = run_check(config)
        stream = sys.stdout if status_code == 0 else sys.stderr
        stream.write(body)
        return status_code

    serve(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
