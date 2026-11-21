"""Live labeled-data capture for the v0.2 eval.

Drives the real demo app with a real browser at two markup releases, records
the page HTML from the last-good release (baseline) and the failing release
(current), and writes failure observations in exactly the shape
phoenix.classify consumes. Labels are true by construction: the scenarios ARE
the planted churn and the planted bug, so eval failures point at the
classifier, never at a shaky fixture.

Run:  python -m phoenix.capture [--out evals/records/live_v2.json]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# scenario: id, label, baseline release, current release, page path with setup,
# the failing probe, expected value.
#   probe kind "css":  {"kind":"css","value":".cart-count"}, step text/count
#   probe kind "name": {"kind":"name","value":"Add to cart","role":"button"}
SCENARIOS = [
    {
        "id": "rot_badge_class",
        "label": "ROT_LOCATOR",
        "baseline_release": "v1",
        "current_release": "v2",
        "path": ["/product/CK-2L", "click_button", "/cart"],
        "probe": {"locator": {"kind": "css", "value": ".cart-count"},
                  "step": "text", "expected": "1"},
    },
    {
        "id": "rot_cart_lines_count",
        "label": "ROT_LOCATOR",
        "baseline_release": "v1",
        "current_release": "v2",
        "path": ["/product/CK-2L", "click_button", "/product/HT-1", "click_button", "/cart"],
        "probe": {"locator": {"kind": "css", "value": ".cart-line"},
                  "step": "count", "expected": 2},
    },
    {
        "id": "copy_buy_button",
        "label": "UI_COPY_CHANGE",
        "baseline_release": "v1",
        "current_release": "v2",
        "path": ["/product/CK-2L"],
        "probe": {"locator": {"kind": "name", "value": "Add to cart", "role": "button"},
                  "step": "visible"},
    },
    {
        "id": "reg_price_kettle",
        "label": "REGRESSION",
        # baseline is v2 (correct price, new design); current is v2reg: same
        # design, kettle price silently changed. Selector resolves; the VALUE
        # is wrong. Classifier must refuse.
        "baseline_release": "v2",
        "current_release": "v2reg",
        "path": ["/product/CK-2L"],
        "probe": {"locator": {"kind": "css", "value": ".p-cost"},
                  "step": "text", "expected": "$48.00"},
    },
    {
        "id": "reg_badge_gone",
        "label": "REGRESSION",
        # hypothetical: v2 minus the badge entirely. We can't remove it from
        # the real app without breaking other scenarios, so current_html is
        # patched below: strip the cc span. Label true by construction.
        "baseline_release": "v1",
        "current_release": "v2",
        "path": ["/cart"],
        "probe": {"locator": {"kind": "css", "value": ".cart-count"},
                  "step": "text", "expected": "0"},
        "patch_current": ("cc", "gone-badge"),
    },
]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server:
    def __init__(self, release: str):
        self.port = _free_port()
        env = {**os.environ, "PYTHONPATH": ROOT, "RELEASE": release}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "demo_app.app:app",
             "--port", str(self.port), "--log-level", "warning"],
            env=env, cwd=ROOT)
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=1):
                    return
            except Exception:
                time.sleep(0.1)
        self.proc.terminate()
        raise RuntimeError(f"server {release} did not start")

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)


def _drive(page, url: str, path: list[str]):
    """Walk the scenario path; 'click_button' performs the add-to-cart click."""
    for step in path:
        if step == "click_button":
            page.get_by_role("button").click()
        else:
            page.goto(url + step)


def _observe(pw, url: str, path: list[str], probe: dict) -> tuple[str, dict]:
    browser = pw.chromium.launch()
    try:
        page = browser.new_page()
        _drive(page, url, path)
        html = page.content()
        loc = probe["locator"]
        observed = None
        if loc["kind"] == "css":
            handle = page.locator(loc["value"])
            n = handle.count()
            if probe["step"] == "count":
                observed = n
            elif probe["step"] == "visible":
                observed = n > 0
            else:  # text
                observed = handle.first.inner_text() if n else None
        else:
            role = loc.get("role", "button")
            n = page.get_by_role(role, name=loc["value"], exact=True).count()
            observed = n > 0
        return html, {"kind": loc["kind"], "step": probe["step"],
                      "expected": probe.get("expected"), "observed": observed}
    finally:
        browser.close()


def _patch_html(html: str, mode: str) -> str:
    import re
    if mode == "gone-badge":
        # page.content() re-serializes the DOM and normalizes quotes to double,
        # which is why the patterns below differ from the app templates.
        out = re.sub(r'<span class="cc">.*?</span>', "", html, count=1)
        if out == html:
            raise RuntimeError("patch_current matched nothing; fixture is lying")
        return out
    raise ValueError(f"unknown patch mode {mode!r}")


def capture_all(pw=None) -> list[dict]:
    """pw: reuse an existing sync_playwright instance (pytest-playwright
    fixture); None opens a private one. Prevents the two-sync-instances
    crash when evals and the page-fixture suite run in one pytest process."""
    releases = sorted({s["baseline_release"] for s in SCENARIOS}
                      | {s["current_release"] for s in SCENARIOS})
    print(f"[capture] starting servers: {releases}", flush=True)
    servers = {}
    records = []
    own = pw is None
    if own:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
    try:
        for r in releases:
            servers[r] = Server(r)
        print("[capture] servers up", flush=True)
        for sc in SCENARIOS:
            print(f"[capture] {sc['id']}: baseline {sc['baseline_release']}", flush=True)
            b_html, _ = _observe(pw, servers[sc["baseline_release"]].url,
                                 sc["path"], sc["probe"])
            print(f"[capture] {sc['id']}: current {sc['current_release']}", flush=True)
            c_html, obs = _observe(pw, servers[sc["current_release"]].url,
                                   sc["path"], sc["probe"])
            if "patch_current" in sc:
                c_html = _patch_html(c_html, sc["patch_current"][1])
            records.append({
                "id": sc["id"], "label": sc["label"],
                "failure": {"locator": sc["probe"]["locator"],
                            "step": sc["probe"]["step"],
                            "expected": sc["probe"].get("expected"),
                            "observed": obs["observed"],
                            "rerun_passed": False},
                "baseline_html": b_html,
                "current_html": c_html,
            })
    finally:
        for s in servers.values():
            s.stop()
        if own and pw is not None:
            pw.stop()          # stop lives on the Playwright object
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    records = capture_all()
    if args.out:
        out = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            json.dump(records, f, indent=1)
        print(f"wrote {len(records)} labeled records to {out}")
    from phoenix.classify import classify_step
    for r in records:
        v = classify_step(r["baseline_html"], r["current_html"], r["failure"])
        ok = "OK " if v.klass == r["label"] else "BAD"
        print(f"{ok} {r['id']:22s} label={r['label']:15s} got={v.klass:15s} "
              f"action={v.action:15s} {v.rationale}")


if __name__ == "__main__":
    sys.path.insert(0, ROOT)
    main()
