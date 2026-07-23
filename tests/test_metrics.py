"""Unit tests for the pure metric helpers in app.py."""

import app as appmod


def test_dbm_to_percent_bounds():
    assert appmod.dbm_to_percent(None) is None
    assert appmod.dbm_to_percent(-30) == 100   # clamped to ceiling
    assert appmod.dbm_to_percent(-50) == 100
    assert appmod.dbm_to_percent(-100) == 0
    assert appmod.dbm_to_percent(-120) == 0     # clamped to floor


def test_dbm_to_percent_linear():
    assert appmod.dbm_to_percent(-75) == 50
    assert appmod.dbm_to_percent(-60) == 80
    assert appmod.dbm_to_percent(-90) == 20


def test_get_system_uptime_is_nonneg_int():
    u = appmod.get_system_uptime()
    assert isinstance(u, int)
    assert u >= 0


def test_get_disks_root():
    disks = appmod.get_disks(["/"])
    assert len(disks) == 1
    d = disks[0]
    assert d["path"] == "/"
    assert d["total"] > 0
    assert 0 <= d["percent"] <= 100


def test_get_disks_skips_bad_path():
    assert appmod.get_disks(["/no/such/path/xyz123"]) == []


def test_get_disks_multiple():
    # "/" is valid, the bogus one is skipped -> exactly one result.
    disks = appmod.get_disks(["/", "/definitely/not/here"])
    assert [d["path"] for d in disks] == ["/"]


def test_downsample_passthrough():
    rows = [(i, float(i), None, None, None, None, None, None) for i in range(10)]
    assert appmod._downsample(rows, max_points=600) == rows


def test_downsample_reduces_and_averages():
    # 1000 rows: a constant column, an all-None column, and another constant.
    rows = [(i, 4.0, None, 2.0, None, None, None, None) for i in range(1000)]
    out = appmod._downsample(rows, max_points=100)
    assert len(out) <= 100
    assert out[0][1] == 4.0     # averaged constant stays constant
    assert out[0][2] is None    # all-None bucket stays None
    assert out[0][3] == 2.0


def test_net_rate_tracker(monkeypatch):
    class FakeNic:
        def __init__(self, rx, tx):
            self.bytes_recv = rx
            self.bytes_sent = tx

    state = {"i": 0}
    snapshots = [
        {"eth0": FakeNic(100, 50), "lo": FakeNic(999, 999)},
        {"eth0": FakeNic(1100, 550), "lo": FakeNic(9999, 9999)},
    ]

    class FakePsutil:
        @staticmethod
        def net_io_counters(pernic=True):
            snap = snapshots[min(state["i"], len(snapshots) - 1)]
            state["i"] += 1
            return snap

    monkeypatch.setattr(appmod, "psutil", FakePsutil)
    monkeypatch.setattr(appmod, "NET_IFACES", [])  # default filter: exclude lo*

    t = appmod.NetRateTracker()
    assert t.sample(now=1000.0) == {}             # first call: no baseline yet
    rates = t.sample(now=1001.0)
    assert "lo" not in rates                       # loopback filtered out
    assert rates["eth0"]["rx_per_s"] == 1000.0     # (1100-100) / 1s
    assert rates["eth0"]["tx_per_s"] == 500.0      # (550-50) / 1s


def test_net_rate_tracker_whitelist(monkeypatch):
    class FakeNic:
        def __init__(self, rx, tx):
            self.bytes_recv = rx
            self.bytes_sent = tx

    class FakePsutil:
        @staticmethod
        def net_io_counters(pernic=True):
            return {"eth0": FakeNic(0, 0), "wlan0": FakeNic(0, 0)}

    monkeypatch.setattr(appmod, "psutil", FakePsutil)
    monkeypatch.setattr(appmod, "NET_IFACES", ["wlan0"])

    t = appmod.NetRateTracker()
    t.sample(now=1.0)
    rates = t.sample(now=2.0)
    assert set(rates.keys()) == {"wlan0"}          # eth0 excluded by whitelist
