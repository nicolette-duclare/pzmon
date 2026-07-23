import os
import tempfile

# Configure the app BEFORE importing it — config is read at import time, and
# importing app.py starts the background sampler. Point it at a throwaway DB and
# push the sampling intervals far out so it doesn't interfere with tests.
_TMPDIR = tempfile.mkdtemp(prefix="pzmon-test-")
os.environ.setdefault("DB_PATH", os.path.join(_TMPDIR, "pzmon.db"))
os.environ.setdefault("SAMPLE_INTERVAL", "3600")
os.environ.setdefault("DB_INTERVAL", "3600")

import pytest

import app as _app


@pytest.fixture
def app_mod():
    return _app


@pytest.fixture
def client():
    _app.app.config.update(TESTING=True)
    with _app.app.test_client() as c:
        yield c
