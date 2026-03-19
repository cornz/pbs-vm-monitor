# pbs-vm-monitor

Small HTTP monitor for Proxmox Backup Server snapshot freshness. It queries the PBS API, evaluates the latest snapshot for each backup target in a datastore, and exposes the result as a plain-text health endpoint that works well with Uptime Kuma, curl, or any other HTTP-based monitor.

This repository is self-contained and has no third-party Python dependencies.

## What It Does

- Fetches snapshot data from the PBS API
- Checks whether the latest snapshot for each backup target is older than a configured threshold
- Returns `200 OK` when the backup is fresh enough
- Returns `500` when the backup is too old or the PBS API cannot be reached
- Supports local configuration through environment variables or a `.env` file

## PBS Setup

Create a dedicated read-only user and API token in Proxmox Backup Server instead of reusing an admin token.

1. Create a user such as `monitoring@pbs`.
2. Give that user permission to audit the target datastore.
   On the datastore path this is typically `/datastore/<your-datastore>`.
   A minimal role is usually `Datastore.Audit`.
3. Create an API token for that user, for example `kuma`, so the token id becomes `monitoring@pbs!kuma`.
4. Copy the token secret shown by PBS when the token is created.

## Quick Start

1. Copy the example configuration:

   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and set your PBS token and desired threshold.
   `PBS_API_TOKEN` must be the same value that comes after `PBSAPIToken=` in your working `curl`, for example `monitoring@pbs!kuma:secret`.

3. Run a one-off check:

   ```bash
   ./check-vm.py check
   ```

4. Start the HTTP server:

   ```bash
   ./check-vm.py serve
   ```

5. Test the endpoint:

   ```bash
   curl -i http://127.0.0.1:8081/health
   ```

## Configuration

The script loads `.env` automatically from the repository root. You can also set environment variables directly in your service manager or shell.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `PBS_BASE_URL` | No | `https://127.0.0.1:8007` | Base URL of your Proxmox Backup Server |
| `PBS_DATASTORE` | No | `offsite` | PBS datastore name used to build the snapshots API path |
| `PBS_API_TOKEN` | Yes | none | Token value after `PBSAPIToken=`, for example `monitoring@pbs!kuma:secret` |
| `PBS_TOKEN_ID` | No | none | Token id such as `monitoring@pbs!kuma` |
| `PBS_TOKEN_SECRET` | No | none | Token secret paired with `PBS_TOKEN_ID` |
| `PBS_AUTHORIZATION` | No | none | Full Authorization header value, with or without the `PBSAPIToken=` prefix |
| `PBS_VERIFY_TLS` | No | `true` | Set to `false` if you use a self-signed certificate |
| `MAX_BACKUP_AGE_HOURS` | No | `24` | Maximum allowed age of the newest snapshot |
| `REQUEST_TIMEOUT_SECONDS` | No | `30` | HTTP timeout for the PBS API request |
| `SERVER_HOST` | No | `0.0.0.0` | Listen address for the built-in HTTP server |
| `SERVER_PORT` | No | `8081` | Listen port for the built-in HTTP server |
| `PBS_VM_MONITOR_ENV_FILE` | No | `.env` next to `check-vm.py` | Optional path to a different env file |

## Usage

Run a one-off check:

```bash
./check-vm.py check
```

Start the server:

```bash
./check-vm.py serve
```

Equivalent working auth configuration examples:

```dotenv
PBS_API_TOKEN=monitoring@pbs!kuma:secret
```

```dotenv
PBS_TOKEN_ID=monitoring@pbs!kuma
PBS_TOKEN_SECRET=secret
```

The root path `/` and `/health` both return plain text such as:

```text
OK: all 2 backup targets are within age limit
OK: vm/100@1710835200 from 2024-03-19T06:40:00+00:00 is 1.25 hours old (limit 24.00h)
OK: vm/101@1710831600 from 2024-03-19T05:40:00+00:00 is 2.25 hours old (limit 24.00h)
```

If any backup target is older than the configured limit, the response body changes to `CRITICAL: ...` and the HTTP status becomes `500`.

## Docker

Build the image:

```bash
docker build -t pbs-vm-monitor .
```

If the container runs on the same Linux host as Proxmox Backup Server, the simplest setup is to keep:

```dotenv
PBS_BASE_URL=https://127.0.0.1:8007
```

and use host networking:

```bash
docker run --rm \
  --name pbs-vm-monitor \
  --network host \
  --env-file .env \
  pbs-vm-monitor
```

Access the monitor at `http://127.0.0.1:8081/health` on the Docker host, or `http://<docker-host-ip>:8081/health` from another machine.

Run a one-off check the same way:

```bash
docker run --rm \
  --network host \
  --env-file .env \
  pbs-vm-monitor check
```

If PBS is on a different machine, set `PBS_BASE_URL` to that machine's real IP or DNS name and use normal port publishing:

```dotenv
PBS_BASE_URL=https://pbs.example.internal:8007
```

```bash
docker run --rm \
  --name pbs-vm-monitor \
  --env-file .env \
  -p 8081:8081 \
  pbs-vm-monitor
```

Access the monitor at `http://127.0.0.1:8081/health` on the Docker host, or `http://<docker-host-ip>:8081/health` from another machine.

In short:

- Same Linux host as PBS: use `--network host`.
- Different host: use the PBS host/IP in `PBS_BASE_URL`.

## Example systemd Service

```ini
[Unit]
Description=PBS VM Monitor
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/pbs-vm-monitor
ExecStart=/usr/bin/python3 /opt/pbs-vm-monitor/check-vm.py serve
Restart=always

[Install]
WantedBy=multi-user.target
```

## Security Notes

- `.env` is ignored by git so tokens stay local.
- Prefer `PBS_VERIFY_TLS=true` when your PBS certificate is trusted.
- The HTTP endpoint does not implement authentication; expose it only to your monitoring network or behind a reverse proxy if needed.

## License

MIT, see [LICENSE](LICENSE).
