"""Model backend contract tests, no API key and no large model needed.

A local fake HTTP server speaks the /v1/chat/completions shape: it asserts
the request carries the bearer key and response_format json_object, returns
a canned OpenAI-shaped reply, and can be told to answer garbage. This proves
OpenAICompatible's wire contract; it does NOT prove any real provider's
behavior. Live verification waits for a key (first task with one)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from phoenix.model import ModelError, OpenAICompatible, get_model


class _Fake(BaseHTTPRequestHandler):
    mode = "ok"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.headers.get("Authorization") == "Bearer test-key-123"
        assert body["response_format"] == {"type": "json_object"}
        assert body["temperature"] == 0
        payload = {"choices": [{"message": {"content": '{"tests": []}'}}]}
        raw = (json.dumps(payload) if _Fake.mode == "ok"
               else json.dumps({"nonsense": True}))
        data = raw.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def fake_server():
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def test_openai_compat_round_trip(fake_server):
    m = OpenAICompatible("fake-model", fake_server, "test-key-123", timeout=10)
    reply = m.complete_json("sys", "user")
    assert json.loads(reply) == {"tests": []}


def test_openai_compat_malformed_reply_raises(fake_server, monkeypatch):
    _Fake.mode = "junk"
    try:
        m = OpenAICompatible("fake-model", fake_server, "test-key-123", timeout=10)
        with pytest.raises(ModelError, match="malformed"):
            m.complete_json("sys", "user")
    finally:
        _Fake.mode = "ok"


def test_get_model_requires_keys(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ModelError, match="OPENAI_API_KEY not set"):
        get_model("openai:gpt-4o-mini")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ModelError, match="OPENROUTER_API_KEY not set"):
        get_model("openrouter:anthropic/claude-sonnet-4.5")
    with pytest.raises(ModelError, match="unknown model spec"):
        get_model("telepathy:crystal-ball")


def test_get_model_openai_honors_base_url(monkeypatch, fake_server):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-123")
    monkeypatch.setenv("OPENAI_BASE_URL", fake_server)
    m = get_model("openai:fake-model")
    assert json.loads(m.complete_json("s", "u")) == {"tests": []}
