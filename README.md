# PZMon

A tiny, dependency-light **Raspberry Pi / Linux system monitor** with a live web dashboard
and persistent history.

PZMon is a small [Flask](https://flask.palletsprojects.com/) app that exposes a self-refreshing
dashboard and a JSON API reporting CPU, memory, disk, temperature, load average, Wi‑Fi signal
strength, network throughput, and the top processes by CPU. A background sampler stores a rolling
time-series in SQLite so you can look back over the last hours/days. It is designed to run in a
container on a Raspberry Pi, but works on any Linux host.

![status](https://img.shields.io/badge/status-alpha-orange) ![python](https://img.shields.io/badge/python-3.11%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Live dashboard** — auto-refreshes (default every 2s); CPU / RAM / Wi‑Fi / network charts.
- **Persistent history** — a background sampler writes metrics to SQLite; switch the charts between
  **Live / 1h / 6h / 24h / 7d** ranges (downsampled server-side).
- **System metrics** — hostname, IP, true system uptime, load average (1/5/15), CPU temperature.
- **CPU & memory** — overall CPU usage %, RAM used/total/%.
- **Multi-disk** — usage for any set of mount points (`DISK_PATHS`).
- **Multi-interface network** — per-interface rx/tx throughput plus an aggregate chart.
- **Wi‑Fi signal** — RSSI in dBm (via `iw`) mapped to a 0–100% scale.
- **Top processes** — the 10 highest-CPU processes with PID, name, CPU %, and RSS.
- **Optional HTTP basic auth** — protect the dashboard/API with a username + password.
- **Health endpoint** — `GET /healthz` for container/uptime checks (never requires auth).
- **Zero JS build step** — a single Jinja template using the Canvas API; no frameworks.

## Requirements

- Python **3.11+**
- [`psutil`](https://pypi.org/project/psutil/) — CPU %, memory, processes, and network counters. The
  app degrades gracefully and returns `null` for those fields if it is missing.
- [`iw`](https://wireless.wiki.kernel.org/en/users/documentation/iw) — only needed for Wi‑Fi signal.
- For Wi‑Fi and host-wide process visibility in Docker: `network_mode: host`, `pid: host`, and the
  `NET_ADMIN` / `NET_RAW` capabilities (already configured in `docker-compose.yml`).

## Quick start

### Run with Docker Compose (recommended on a Pi)

```bash
docker compose up -d --build
```

Then open **http://<pi-ip>:18080**. History is persisted in the `pzmon-data` volume (`/data/pzmon.db`).

### Run locally (no Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open **http://localhost:18080**.

> On non-Linux hosts, CPU-temperature and Wi‑Fi fields will be `null`/`-` (those read Linux-specific
> sources). Everything else still works.

## Configuration

All configuration is via environment variables:

| Variable          | Default     | Description                                                        |
|-------------------|-------------|-------------------------------------------------------------------|
| `PORT`            | `18080`     | HTTP listen port.                                                 |
| `REFRESH_MS`      | `2000`      | Dashboard auto-refresh interval (ms).                             |
| `HISTORY_LEN`     | `60`        | Number of points kept in the in-browser "Live" charts.           |
| `DISK_PATHS`      | `/`         | Comma-separated mount points to report (e.g. `/,/boot`).          |
| `WIFI_IFACE`      | `wlan0`     | Wireless interface to query for signal strength via `iw`.         |
| `NET_IFACES`      | *(all)*     | Comma-separated interface whitelist; default is all except `lo*`. |
| `SAMPLE_INTERVAL` | `2`         | How often (s) the background sampler collects metrics.            |
| `DB_INTERVAL`     | `10`        | How often (s) a sample is written to SQLite.                      |
| `DB_PATH`         | `pzmon.db`  | Path to the SQLite history database (`/data/pzmon.db` in Docker). |
| `RETENTION_HOURS` | `168`       | How long (h) to keep history before pruning (default 7 days).     |
| `PZMON_USER`      | *(unset)*   | If set together with `PZMON_PASSWORD`, enables HTTP basic auth.   |
| `PZMON_PASSWORD`  | *(unset)*   | Password for basic auth.                                          |

## API

### `GET /api/status`

Latest metrics snapshot (served from the sampler's in-memory cache, so it's cheap).

```jsonc
{
  "ts": 1718971200000,             // snapshot time (ms epoch)
  "hostname": "raspberrypi",
  "ip": "192.168.1.42",
  "system_uptime_seconds": 184230, // true system uptime (/proc/uptime)
  "process_uptime_seconds": 1234,  // how long PZMon has been running
  "cpu_temp_c": 47.8,              // null if unavailable
  "load1": 0.12, "load5": 0.20, "load15": 0.18,
  "disks": [
    { "path": "/", "total": 31901171712, "used": 6553600000, "percent": 20.5 }
  ],
  "wifi_iface": "wlan0",
  "wifi_signal_dbm": -47,          // null if unavailable
  "wifi_signal_percent": 100,
  "net": {
    "wlan0": { "rx_per_s": 1373.9, "tx_per_s": 865.1 }   // bytes/sec
  },
  "net_rx_per_s": 1373.9,
  "net_tx_per_s": 865.1,
  "cpu_usage_percent": 3.4,        // null if psutil is missing
  "mem_total": 4127203328,
  "mem_used": 812345678,
  "mem_percent": 19.7,
  "top_processes": [
    { "pid": 1234, "name": "python3", "cpu": 2.1, "rss": 51200000 }
  ],
  "psutil": true
}
```

### `GET /api/history?range=<1h|6h|24h|7d>`

Returns a downsampled time-series from SQLite (≤ ~600 points per range):

```jsonc
{
  "range": "1h",
  "ts":   [1718971200000, ...],   // ms epoch
  "cpu":  [3.4, ...],             // %
  "mem":  [19.7, ...],           // %
  "temp": [47.8, ...],           // °C
  "wifi": [100, ...],            // %
  "load1":[0.12, ...],
  "rx_per_s": [1373.9, ...],     // bytes/sec
  "tx_per_s": [865.1, ...]
}
```

### `GET /healthz`

Liveness/readiness probe (never requires auth):

```json
{ "status": "ok", "psutil": true, "sampler_running": true, "last_sample_age_s": 1.6, "auth_enabled": false }
```

### `GET /`

Serves the HTML dashboard.

## How it works

- A **background sampler thread** collects a full metrics snapshot every `SAMPLE_INTERVAL` seconds,
  caches the latest in memory (what `/api/status` returns), and writes a row to SQLite every
  `DB_INTERVAL` seconds, pruning anything older than `RETENTION_HOURS`.
- Centralizing sampling in one thread keeps `psutil.cpu_percent()` windows consistent and makes the
  HTTP handlers cheap.
- Metric sources: `/proc/uptime` (uptime), `os.getloadavg()` / `shutil.disk_usage()` (stdlib),
  `/sys/class/thermal/thermal_zone0/temp` (CPU temp), `iw dev <iface> link` (Wi‑Fi RSSI), and
  `psutil` (CPU %, memory, per-interface counters, processes).

## Project layout

```
pzmon/
├── app.py                 # Flask app: config, sampler, SQLite, routes
├── templates/
│   └── index.html         # dashboard (Jinja + Canvas charts)
├── tests/                 # pytest suite (metric helpers + API/auth)
│   ├── test_metrics.py
│   └── test_api.py
├── conftest.py            # test fixtures (isolated DB, Flask test client)
├── pytest.ini
├── requirements.txt       # pinned Flask + psutil
├── requirements-dev.txt   # requirements.txt + pytest
├── Dockerfile             # python:3.11-slim + iw; /data volume for the DB
├── docker-compose.yml     # host network + host pid + NET_ADMIN/NET_RAW + volume
├── .dockerignore
├── LICENSE                # MIT
└── README.md
```

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite covers the pure metric helpers (`dbm_to_percent`, `get_disks`, `_downsample`,
`NetRateTracker`) and the HTTP layer (`/api/status`, `/api/history`, `/healthz`, and the basic-auth
gate) via Flask's test client. Tests run against an isolated temporary SQLite database, so they don't
touch your real history.

## Notes & caveats

- There is **no transport encryption**. Basic auth protects access but credentials are sent in the
  clear over HTTP — keep PZMon on a trusted LAN or behind a TLS-terminating reverse proxy.
- Network rates in `/api/status` are computed by the shared sampler; with multiple simultaneous
  dashboard viewers the *live* numbers stay correct because rates come from one place.
- The history DB lives at `DB_PATH` (a Docker volume by default) and survives restarts.

## License

[MIT](LICENSE) © 2026 Vladislav A
