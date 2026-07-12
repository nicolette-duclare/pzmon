"""Tests for the HTTP endpoints, history persistence, and the auth gate."""

import base64
import time


def _basic(user, pwd):
    token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "ok"
    assert "sampler_running" in body
    assert "last_sample_age_s" in body


def test_status_returns_latest(client, app_mod):
    app_mod.set_latest({"ts": 123, "hostname": "unit-test", "disks": [], "net": {}})
    r = client.get("/api/status")
    assert r.status_code == 200
    assert r.get_json()["hostname"] == "unit-test"


def test_collect_status_shape(app_mod):
    snap = app_mod.collect_status(app_mod.NetRateTracker())
    for key in ("ts", "hostname", "ip", "system_uptime_seconds",
                "process_uptime_seconds", "disks", "net", "load1"):
        assert key in snap
    assert isinstance(snap["disks"], list)
    assert isinstance(snap["net"], dict)


def test_history_returns_rows(client, app_mod, tmp_path, monkeypatch):
    db = tmp_path / "hist.db"
    monkeypatch.setattr(app_mod, "DB_PATH", str(db))
    app_mod.db_init()

    conn = app_mod.db_connect()
    now = int(time.time())
    for i in range(5):
        conn.execute(
            "INSERT INTO samples (ts, cpu, mem, temp, wifi, load1, rx_per_s, tx_per_s) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now - i * 10, 10.0 + i, 20.0 + i, None, 50, 0.1, 100.0, 50.0),
        )
    conn.commit()
    conn.close()

    r = client.get("/api/history?range=1h")
    assert r.status_code == 200
    body = r.get_json()
    assert body["range"] == "1h"
    assert len(body["ts"]) == 5
    assert len(body["cpu"]) == 5
    assert body["ts"] == sorted(body["ts"])        # ascending by timestamp


def test_history_excludes_out_of_range(client, app_mod, tmp_path, monkeypatch):
    db = tmp_path / "hist3.db"
    monkeypatch.setattr(app_mod, "DB_PATH", str(db))
    app_mod.db_init()

    conn = app_mod.db_connect()
    now = int(time.time())
    conn.execute(
        "INSERT INTO samples (ts, cpu, mem, temp, wifi, load1, rx_per_s, tx_per_s) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (now - 10, 1.0, 1.0, None, None, None, None, None),       # in range
    )
    conn.execute(
        "INSERT INTO samples (ts, cpu, mem, temp, wifi, load1, rx_per_s, tx_per_s) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (now - 7200, 2.0, 2.0, None, None, None, None, None),     # 2h old, outside 1h
    )
    conn.commit()
    conn.close()

    body = client.get("/api/history?range=1h").get_json()
    assert len(body["ts"]) == 1


def test_history_invalid_range_is_empty_db(client, app_mod, tmp_path, monkeypatch):
    db = tmp_path / "hist2.db"
    monkeypatch.setattr(app_mod, "DB_PATH", str(db))
    app_mod.db_init()
    r = client.get("/api/history?range=banana")
    assert r.status_code == 200
    assert r.get_json()["ts"] == []


def test_auth_gate(client, app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "AUTH_ENABLED", True)
    monkeypatch.setattr(app_mod, "AUTH_USER", "pi")
    monkeypatch.setattr(app_mod, "AUTH_PASSWORD", "secret")

    # no credentials -> 401 + challenge header
    r = client.get("/api/status")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers

    # wrong credentials -> 401
    assert client.get("/api/status", headers=_basic("pi", "nope")).status_code == 401

    # correct credentials -> 200
    app_mod.set_latest({"ts": 1, "hostname": "x"})
    assert client.get("/api/status", headers=_basic("pi", "secret")).status_code == 200

    # health check stays open even with auth enabled
    assert client.get("/healthz").status_code == 200
