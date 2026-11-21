"""Bring-your-own model interface for the drafter. Deliberately tiny.

Backends:
  Scripted  in-process canned replies; the test double and the key-free demo.
  Ollama    local HTTP (format=json constrained output; the open_otto lesson
            is that plain prompting on small models wastes most runs on
            broken JSON).
Nothing here does retries beyond one repair round; the CALLER owns judging
the reply. Models propose, code disposes.
"""
from __future__ import annotations

import json
import urllib.request


class ModelError(Exception):
    pass


class Scripted:
    name = "scripted"

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.prompts: list[tuple[str, str]] = []

    def complete_json(self, system: str, user: str) -> str:
        self.prompts.append((system, user))
        if not self.replies:
            raise ModelError("scripted model out of replies")
        return self.replies.pop(0)


class Ollama:
    def __init__(self, model: str, host: str = "http://localhost:11434",
                 timeout: int = 300):
        self.name = f"ollama:{model}"
        self.model = model
        self.url = f"{host}/api/chat"
        self.timeout = timeout

    def complete_json(self, system: str, user: str) -> str:
        body = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        req = urllib.request.Request(
            self.url, json.dumps(body).encode(),
            {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode())
        except Exception as e:
            raise ModelError(f"ollama unreachable: {e}")
        return data.get("message", {}).get("content", "")


class OpenAICompatible:
    """One backend for every /v1/chat/completions-shaped API that supports
    response_format json_object: OpenAI itself, OpenRouter, and local
    servers. Key comes from the environment; never printed, never logged.

    Contract-tested against a local fake HTTP server, and live-verified
    through OpenRouter (free-tier and paid routes). Paid models on a
    zero-credit account answer 402; run with
    PHOENIX_LLM=openrouter:<model>:free or fund the account.
    Keys come from the environment only and are never printed or logged."""

    def __init__(self, model: str, base: str, key: str, timeout: int = 180):
        self.name = f"openai-compat:{model}"
        self.model = model
        self.url = base.rstrip("/") + "/chat/completions"
        self.key = key
        self.timeout = timeout

    def complete_json(self, system: str, user: str) -> str:
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        req = urllib.request.Request(
            self.url, json.dumps(body).encode(),
            {"Content-Type": "application/json",
             "Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode())
        except Exception as e:
            raise ModelError(f"{self.name} request failed: {e}")
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            raise ModelError(f"{self.name} malformed reply")


def get_model(spec: str):
    """spec: 'scripted', 'ollama:<model>', 'openai:<model>' (OPENAI_API_KEY,
    optional OPENAI_BASE_URL), 'openrouter:<model>' (OPENROUTER_API_KEY)."""
    import os
    if spec == "scripted":
        return Scripted([])
    if spec.startswith("ollama:"):
        return Ollama(spec.split(":", 1)[1])
    if spec.startswith("openai:"):
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise ModelError("OPENAI_API_KEY not set")
        base = os.environ.get(
            "OPENAI_BASE_URL", "https://api.openai.com/v1")
        return OpenAICompatible(spec.split(":", 1)[1], base, key)
    if spec.startswith("openrouter:"):
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            raise ModelError("OPENROUTER_API_KEY not set")
        return OpenAICompatible(spec.split(":", 1)[1],
                                "https://openrouter.ai/api/v1", key)
    raise ModelError(f"unknown model spec {spec!r}")
