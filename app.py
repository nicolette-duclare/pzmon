import os
import re
import time
import math
import shutil
import sqlite3
import hmac
import threading
import subprocess

from flask import Flask, jsonify, render_template, request, Response

try:
    import psutil
except Exception:
    psutil = None

app = Flask(__name__)
START_TIME = time.time()


# --------------------------------------------------------------------------- #
# Configuration (environment variables)
# --------------------------------------------------------------------------- #
def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


PORT = _env_int("PORT", 18080)
REFRESH_MS = _env_int("REFRESH_MS", 2000)
HISTORY_LEN = _env_int("HISTORY_LEN", 60)
DISK_PATHS = [p.strip() for p in os.environ.get("DISK_PATHS", "/").split(",") if p.strip()]
WIFI_IFACE = os.environ.get("WIFI_IFACE", "wlan0")
# Optional whitelist of network interfaces; default = all except loopback.
NET_IFACES = [i.strip() for i in os.environ.get("NET_IFACES", "").split(",") if i.strip()]

SAMPLE_INTERVAL = _env_float("SAMPLE_INTERVAL", 2.0)   # how often we sample metrics
DB_INTERVAL = _env_float("DB_INTERVAL", 10.0)          # how often we persist a sample
DB_PATH = os.environ.get("DB_PATH", "pzmon.db")
RETENTION_HOURS = _env_int("RETENTION_HOURS", 168)     # 7 days

AUTH_USER = os.environ.get("PZMON_USER")
AUTH_PASSWORD = os.environ.get("PZMON_PASSWORD")
AUTH_ENABLED = bool(AUTH_USER and AUTH_PASSWORD)

HISTORY_RANGES = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 24 * 3600,
}


# --------------------------------------------------------------------------- #
# Low-level metric helpers
# --------------------------------------------------------------------------- #
def _run(cmd):
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def get_ip():
    out = _run(["hostname", "-I"])
    if not out:
        return "unknown"
    parts = out.split()
    return parts[0] if parts else "unknown"


def get_cpu_temp_c():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return None


def get_system_uptime():
    """System uptime in seconds (from /proc/uptime), falling back to process uptime."""
    try:
        with open("/proc/uptime", "r") as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return int(time.time() - START_TIME)


def wifi_signal_dbm(iface="wlan0"):
    """RSSI in dBm as int, or None. Uses: iw dev <iface> link -> 'signal: -47 dBm'."""
    out = _run(["iw", "dev", iface, "link"])
    if not out:
        return None
    m = re.search(r"signal:\s*(-?\d+)\s*dBm", out)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def dbm_to_percent(dbm):
    if dbm is None:
        return None
    if dbm <= -100:
        return 0
    if dbm >= -50:
        return 100
    return int(round(2 * (dbm + 100)))  # linear between -100..-50


def get_disks(paths):
    disks = []
    for p in paths:
        try:
            du = shutil.disk_usage(p)
        except Exception:
            continue
        disks.append({
            "path": p,
            "total": int(du.total),
            "used": int(du.used),
            "percent": round((du.used / du.total) * 100, 1) if du.total else None,
        })
    return disks


class NetRateTracker:
    """Computes per-interface rx/tx byte rates between successive samples."""

    def __init__(self):
        self._prev = None  # (timestamp, {iface: (rx, tx)})

    @staticmethod
    def _keep(iface):
        if NET_IFACES:
            return iface in NET_IFACES
        return not iface.startswith("lo")

    def sample(self, now=None):
        if not psutil:
            return {}
        if now is None:
            now = time.time()
        try:
            counters = psutil.net_io_counters(pernic=True)
        except Exception:
            return {}
        cur = {
            nic: (c.bytes_recv, c.bytes_sent)
            for nic, c in counters.items()
            if self._keep(nic)
        }
        rates = {}
        if self._prev:
            pt, pv = self._prev
            dt = now - pt
            if dt > 0:
                for nic, (rx, tx) in cur.items():
                    if nic in pv:
                        prx, ptx = pv[nic]
                        rates[nic] = {
                            "rx_per_s": max(0.0, (rx - prx) / dt),
                            "tx_per_s": max(0.0, (tx - ptx) / dt),
                        }
        self._prev = (now, cur)
        return rates


def top_processes(limit=10):
    if not psutil:
        return []
    for p in psutil.process_iter(attrs=["pid", "name"]):
        try:
            p.cpu_percent(None)
        except Exception:
            pass
    time.sleep(0.05)
    procs = []
    for p in psutil.process_iter(attrs=["pid", "name", "memory_info"]):
        try:
            cpu = p.cpu_percent(None)
            rss = getattr(p.info.get("memory_info"), "rss", 0) if p.info.get("memory_info") else 0
            procs.append({
                "pid": p.info["pid"],
                "name": p.info.get("name") or "?",
                "cpu": round(cpu, 1),
                "rss": int(rss),
            })
        except Exception:
            continue
    procs.sort(key=lambda x: x["cpu"], reverse=True)
    return procs[:limit]


def collect_status(net_tracker):
    """Build a full metrics snapshot."""
    try:
        load1, load5, load15 = os.getloadavg()
        load1, load5, load15 = round(load1, 2), round(load5, 2), round(load15, 2)
    except Exception:
        load1 = load5 = load15 = None

    sig_dbm = wifi_signal_dbm(WIFI_IFACE)
    net = net_tracker.sample() if net_tracker else {}
    net_rx = round(sum(r["rx_per_s"] for r in net.values()), 1) if net else 0.0
    net_tx = round(sum(r["tx_per_s"] for r in net.values()), 1) if net else 0.0

    result = {
        "ts": int(time.time() * 1000),
        "hostname": _run(["hostname"]) or "unknown",
        "ip": get_ip(),
        "system_uptime_seconds": get_system_uptime(),
        "process_uptime_seconds": int(time.time() - START_TIME),
        "cpu_temp_c": get_cpu_temp_c(),
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "disks": get_disks(DISK_PATHS),
        "wifi_iface": WIFI_IFACE,
        "wifi_signal_dbm": sig_dbm,
        "wifi_signal_percent": dbm_to_percent(sig_dbm),
        "net": {nic: {"rx_per_s": round(v["rx_per_s"], 1), "tx_per_s": round(v["tx_per_s"], 1)}
                for nic, v in net.items()},
        "net_rx_per_s": net_rx,
        "net_tx_per_s": net_tx,
        "cpu_usage_percent": None,
        "mem_total": None,
        "mem_used": None,
        "mem_percent": None,
        "top_processes": [],
        "psutil": psutil is not None,
    }

    if psutil:
        try:
            result["cpu_usage_percent"] = round(psutil.cpu_percent(interval=None), 1)
        except Exception:
            pass
        try:
            vm = psutil.virtual_memory()
            result["mem_total"] = int(vm.total)
            result["mem_used"] = int(vm.used)
            result["mem_percent"] = round(vm.percent, 1)
        except Exception:
            pass
        result["top_processes"] = top_processes()

    return result


# --------------------------------------------------------------------------- #
# Latest-snapshot cache (written by the sampler thread, read by HTTP handlers)
# --------------------------------------------------------------------------- #
_latest_lock = threading.Lock()
_latest = {"ts": 0}


def set_latest(snapshot):
    global _latest
    with _latest_lock:
        _latest = snapshot


def get_latest():
    with _latest_lock:
        return dict(_latest)


# --------------------------------------------------------------------------- #
# Persistent time-series (SQLite)
# --------------------------------------------------------------------------- #
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def db_init():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = db_connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS samples (
            ts       INTEGER PRIMARY KEY,
            cpu      REAL,
            mem      REAL,
            temp     REAL,
            wifi     REAL,
            load1    REAL,
            rx_per_s REAL,
            tx_per_s REAL
        )
        """
    )
    conn.commit()
    conn.close()


def db_write(conn, snap):
    conn.execute(
        "INSERT OR REPLACE INTO samples (ts, cpu, mem, temp, wifi, load1, rx_per_s, tx_per_s) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(snap["ts"] / 1000),
            snap.get("cpu_usage_percent"),
            snap.get("mem_percent"),
            snap.get("cpu_temp_c"),
            snap.get("wifi_signal_percent"),
            snap.get("load1"),
            snap.get("net_rx_per_s"),
            snap.get("net_tx_per_s"),
        ),
    )
    cutoff = int(time.time()) - RETENTION_HOURS * 3600
    conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
    conn.commit()


def _downsample(rows, max_points=600):
    n = len(rows)
    if n <= max_points:
        return rows
    bucket = math.ceil(n / max_points)
    out = []
    for i in range(0, n, bucket):
        chunk = rows[i:i + bucket]
        cols = list(zip(*chunk))
        ts = chunk[len(chunk) // 2][0]
        averaged = [ts]
        for col in cols[1:]:
            vals = [v for v in col if v is not None]
            averaged.append(round(sum(vals) / len(vals), 2) if vals else None)
        out.append(tuple(averaged))
    return out


# --------------------------------------------------------------------------- #
# Background sampler
# --------------------------------------------------------------------------- #
def sampler_loop():
    net_tracker = NetRateTracker()
    if psutil:
        try:
            psutil.cpu_percent(interval=None)  # prime the global CPU counter
        except Exception:
            pass
    net_tracker.sample()  # prime net counters

    conn = db_connect()
    last_db = 0.0
    while True:
        try:
            snap = collect_status(net_tracker)
            set_latest(snap)
            now = time.time()
            if now - last_db >= DB_INTERVAL:
                db_write(conn, snap)
                last_db = now
        except Exception:
            pass
        time.sleep(SAMPLE_INTERVAL)


_sampler_started = False
_sampler_lock = threading.Lock()


def start_background():
    global _sampler_started
    with _sampler_lock:
        if _sampler_started:
            return
        db_init()
        # Seed an initial snapshot so the first page load has data.
        try:
            set_latest(collect_status(NetRateTracker()))
        except Exception:
            pass
        threading.Thread(target=sampler_loop, daemon=True).start()
        _sampler_started = True


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _auth_ok(auth):
    if not AUTH_ENABLED:
        return True
    if not auth or auth.username is None or auth.password is None:
        return False
    return (
        hmac.compare_digest(auth.username, AUTH_USER)
        and hmac.compare_digest(auth.password, AUTH_PASSWORD)
    )


@app.before_request
def _auth_gate():
    if request.path == "/healthz" or not AUTH_ENABLED:
        return
    if not _auth_ok(request.authorization):
        return Response(
            "Authentication required.\n",
            401,
            {"WWW-Authenticate": 'Basic realm="PZMon"'},
        )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return render_template(
        "index.html",
        config={
            "refresh_ms": REFRESH_MS,
            "history_len": HISTORY_LEN,
            "ranges": list(HISTORY_RANGES.keys()),
        },
    )


@app.get("/api/status")
def status():
    return jsonify(get_latest())


@app.get("/api/history")
def history():
    range_key = request.args.get("range", "1h")
    seconds = HISTORY_RANGES.get(range_key, HISTORY_RANGES["1h"])
    since = int(time.time()) - seconds

    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT ts, cpu, mem, temp, wifi, load1, rx_per_s, tx_per_s "
            "FROM samples WHERE ts >= ? ORDER BY ts",
            (since,),
        ).fetchall()
    finally:
        conn.close()

    rows = _downsample(rows)
    return jsonify({
        "range": range_key,
        "ts": [r[0] * 1000 for r in rows],
        "cpu": [r[1] for r in rows],
        "mem": [r[2] for r in rows],
        "temp": [r[3] for r in rows],
        "wifi": [r[4] for r in rows],
        "load1": [r[5] for r in rows],
        "rx_per_s": [r[6] for r in rows],
        "tx_per_s": [r[7] for r in rows],
    })


@app.get("/healthz")
def healthz():
    latest = get_latest()
    last_ts = latest.get("ts", 0)
    age = round((time.time() * 1000 - last_ts) / 1000, 1) if last_ts else None
    return jsonify({
        "status": "ok",
        "psutil": psutil is not None,
        "sampler_running": _sampler_started,
        "last_sample_age_s": age,
        "auth_enabled": AUTH_ENABLED,
    })


# Start the sampler as soon as the module is imported (covers `flask run`,
# `python app.py`, and WSGI servers alike).
start_background()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
