"""v0.3 end-to-end: run the full pipeline once per session and assert the
contract. Slow (live capture + three sandbox suite runs), so it is marked and
the fast adversarial guard tests live in test_v03_guard.py.

The pipeline test asserts:
  - all 5 labeled records classified
  - exactly the 3 rot/copy records become changes, the 2 regressions do not
  - verification reports three consecutive green sandbox runs at v2
  - the bundle contains diff/evidence/meta/pr.md and applied_to_working_tree
    is False in meta (the bot never merges)
  - the live suite on disk is byte-identical before and after (proof)
"""
import hashlib
import json
import os

import pytest

pytestmark = pytest.mark.slow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUITE = os.path.join(ROOT, "tests", "test_storefront.py")


@pytest.fixture(scope="session")
def pipeline(playwright):
    from phoenix.patcher import run_pipeline
    before = hashlib.sha256(open(SUITE, "rb").read()).hexdigest()
    out = run_pipeline(release="v2", pw=playwright)
    out["_suite_hash_before"] = before
    return out


def test_classification_outcome(pipeline):
    res = {r["id"]: r for r in pipeline["results"]}
    assert set(res) == {"rot_badge_class", "rot_cart_lines_count",
                        "copy_buy_button", "reg_price_kettle", "reg_badge_gone"}
    for rid in ("rot_badge_class", "rot_cart_lines_count", "copy_buy_button"):
        assert res[rid]["patched"], rid
    for rid in ("reg_price_kettle", "reg_badge_gone"):
        assert not res[rid]["patched"] and res[rid]["class"] == "REGRESSION", rid
    assert pipeline["rejected"] is None


def test_verification_gate(pipeline):
    v = pipeline["verification"]
    assert v and v["all_passed"]
    assert len(v["runs"]) == 3
    assert all(r["exit"] == 0 for r in v["runs"])


def test_bundle_written_and_nothing_applied(pipeline):
    bdir = os.path.join(ROOT, "prs", pipeline["bundle"])
    for name in ("pr.md", "diff.patch", "meta.json", "verify.log", "audit.jsonl"):
        assert os.path.exists(os.path.join(bdir, name)), name
    meta = json.load(open(os.path.join(bdir, "meta.json")))
    assert meta["applied_to_working_tree"] is False
    assert len(meta["patches"]) == 3
    assert len(meta["bug_reports"]) == 2


def test_live_suite_untouched(pipeline):
    after = hashlib.sha256(open(SUITE, "rb").read()).hexdigest()
    assert after == pipeline["_suite_hash_before"]


def test_diff_is_locators_only(pipeline):
    bdir = os.path.join(ROOT, "prs", pipeline["bundle"])
    diff = open(os.path.join(bdir, "diff.patch")).read()
    changed = [l for l in diff.splitlines()
               if (l.startswith("+") or l.startswith("-"))
               and not l.startswith(("+++", "---"))]
    for line in changed:
        # every touched line contains a locator call; no assertion arg, no
        # def, no import, no skip
        assert "locator(" in line or "get_by_role(" in line, line
        assert "def " not in line and "skip" not in line and "import" not in line
