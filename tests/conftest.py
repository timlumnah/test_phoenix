"""Boots the demo app on a free port. RELEASE from the environment (v1 default).

Run the suite against the redesigned markup with:
    RELEASE=v2 python -m pytest -q
v1: all tests pass. v2: the class-coupled tests fail, label/role/text-based
tests pass. That split is the labeled data the v0.2 classifier trains against.
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.environ.get("TEST_PHOENIX_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def server_url():
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": ROOT}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "demo_app.app:app",
         "--port", str(port), "--log-level", "warning"],
        env=env, cwd=ROOT,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("demo app did not become ready")
    yield url
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="session")
def base_url(server_url):
    """Consumed by pytest-playwright's context; page.goto('/') resolves here."""
    return server_url
