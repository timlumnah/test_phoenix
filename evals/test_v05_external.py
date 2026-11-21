"""v0.5 eval: the pipeline against a stand-in THIRD-PARTY app.

The mini app (evals/extmini_app.py) reproduces the real-world shapes the
demo app never presented: relative hrefs, same-origin absolute hrefs, a
<select> in a labeled form, an HTTP-basic-auth wall, a cross-origin link,
and an action page that changes after its button click. These tests are
key-free and network-free: the 'third party' is a local server. The live
external run against a real public app is the draft_cli --url CLI, measured
in README/instructions, not repeated in CI.
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVALS = os.path.join(ROOT, "evals")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def mini_url():
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([ROOT, EVALS])}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "extmini_app:app",
         "--port", str(port), "--log-level", "warning"],
        env=env, cwd=ROOT)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"{url}/", timeout=1):
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("extmini did not start")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# --- pure logic: href resolution (no browser) --------------------------------

@pytest.mark.parametrize("href,page_url,expect", [
    ("one", "http://h/one", "/one"),              # replaces last segment, like a browser
    ("one", "http://h/sub/deep", "/sub/one"),
    ("one/", "http://h/sub/deep", "/sub/one/"),
    ("../one", "http://h/sub/deep", "/one"),
    ("/abs", "http://h/sub/deep", "/abs"),
    ("http://h/x?q=1#f", "http://h/", "/x"),
    ("#top", "http://h/", None),
    ("mailto:a@b.c", "http://h/", None),
    ("http://elsewhere.invalid/x", "http://h/", None),
])
def test_resolve_href_browser_style(href, page_url, expect):
    from phoenix.explore import resolve_href
    assert resolve_href(href, page_url, "http://h") == expect


def test_gate_rejects_fill_on_select_field():
    """The rule drafter can never emit it (it skips select labels), so the
    ceiling rule says a model may not either: fill() throws on <select>."""
    from phoenix.draft import check_draft
    obs = {"h1": "Form", "buttons": ["Save"], "links": [], "labels": ["Color", "Note"],
           "select_labels": ["Color"], "link_pairs": [], "main_texts": []}
    catalog = {"origin": "http://x", "pages": {"/f": obs}, "probes": {}, "flow": {}}
    bad = {"tests": [{"name": "test_bad", "steps": [
        {"goto": "/f"}, {"fill_label": ["Color", "red"]}]}]}
    ok = {"tests": [{"name": "test_ok", "steps": [
        {"goto": "/f"}, {"fill_label": ["Note", "hello"]}]}]}
    assert any("<select>" in s for s in check_draft(catalog, bad))
    assert check_draft(catalog, ok) == []


# --- live-ish: explore the third-party stand-in ------------------------------

@pytest.fixture(scope="module")
def external_catalog(playwright, mini_url):
    from phoenix.explore import explore
    return explore(mini_url, pw=playwright,
                   flow_policy=lambda p, o: p == "/act")


def test_relative_and_path_hrefs_all_crawled(external_catalog):
    pages = set(external_catalog["pages"])
    assert {"/", "/one", "/abs", "/sub/deep", "/act"} <= pages


def test_cross_origin_link_not_followed(external_catalog):
    assert not any("elsewhere" in p or "invalid" in p
                   for p in external_catalog["pages"])


def test_auth_wall_does_not_kill_the_crawl(external_catalog):
    cat = external_catalog
    # either goto failed (skipped) or it recorded an empty shell; both prove
    # resilience. What must NOT happen: a crash, or probes/flows on /secret.
    assert "/secret" in cat["skipped"] or not cat["pages"].get("/secret", {}).get("h1")
    assert not any(k.startswith("/secret") for k in cat["probes"])
    assert set(cat["pages"]) >= {"/", "/one", "/act"}   # crawl completed


def test_select_field_is_seen_and_probed(external_catalog):
    cat = external_catalog
    assert "Favourite color" in cat["pages"]["/one"]["select_labels"]
    probe = cat["probes"]["/one#0"]
    assert "Please add a note first" in probe["empty_result"]["obs"]["main_texts"]
    assert probe["valid_result"]["path"] == "/done"     # redirect observed
    assert probe["valid_result"]["obs"]["h1"] == "All Done"
    assert not [e for e in cat["probe_errors"] if e.startswith("/one")]


def test_flow_policy_picks_the_action_page(external_catalog):
    cat = external_catalog
    assert "/act" in cat["flow"] and "/one" not in cat["flow"]
    fl = cat["flow"]["/act"]
    assert fl["clicked_button"] == "Add Thing"
    assert "Remove Thing" in fl["landing_page"]["buttons"]
    # the click CHANGED the page: merged back into the page record
    assert "Thing added" in cat["pages"]["/act"]["main_texts"]


def test_rule_draft_passes_gate_on_external_catalog(external_catalog):
    from phoenix.draft import check_draft, rule_draft
    draft = rule_draft(external_catalog)
    assert draft["tests"], "rule drafter produced nothing"
    assert check_draft(external_catalog, draft) == []


@pytest.mark.slow
def test_external_suite_verifies_3x_against_live_url(external_catalog, mini_url):
    """The v0.5 breadth claim in miniature: drafted from observations only,
    rendered deterministically, 3 consecutive green runs against a URL the
    harness does not boot, via the external sandbox conftest."""
    from phoenix.draft import check_draft, render, rule_draft
    from phoenix.patcher import EXTERNAL_CONFTEST, Audit, verify_sandboxed
    draft = rule_draft(external_catalog)
    assert check_draft(external_catalog, draft) == []
    source = render(draft)
    audit_path = os.path.join(ROOT, "drafts", "eval-external-audit.jsonl")
    os.makedirs(os.path.dirname(audit_path), exist_ok=True)
    audit = Audit(audit_path)
    try:
        ver = verify_sandboxed(source, "v1", audit,
                               filename="draft_test_ext_eval.py",
                               conftest_text=EXTERNAL_CONFTEST,
                               extra_env={"PHOENIX_TARGET_URL": mini_url})
    finally:
        audit.close()
    assert ver["all_passed"], ver["log"][-2000:]
    assert len(ver["runs"]) == 3
