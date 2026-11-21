"""v0.3 patch agent: classify -> guarded patch -> re-run gate -> PR bundle.

Flow, as specified in the README:
1. capture: live labeled failures (phoenix.capture)
2. classify: verdicts (phoenix.classify); only ROT_LOCATOR and UI_COPY_CHANGE
   are patchable. REGRESSION records become bug reports and touch no test.
3. patch: one combined guarded_patch over the suite (phoenix.astpatch). The
   agent proposes selector strings; the guard decides what can land.
4. verify: run the PATCHED suite in a sandbox, in isolation from the live
   tree, three consecutive times at the failing release; pass requires the
   full suite green all three times (previous failures fixed, rest unchanged).
5. bundle: prs/<name>/ with diff, evidence html, classifier rationale,
   three re-run logs, audit.jsonl, and a ready gh pr create body. Nothing
   is applied to the working tree and nothing is published: merging is the
   human command, written into pr.md.

Run:  python -m phoenix.patcher
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import shutil
import subprocess
import sys

from phoenix.astpatch import PatchRejected, guarded_patch
from phoenix.capture import capture_all
from phoenix.classify import ACTION_PROPOSE, classify_step

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUITE = os.path.join(ROOT, "tests", "test_storefront.py")
PRS_DIR = os.path.join(ROOT, "prs")
SANDBOX = os.path.join(ROOT, ".pr_sandbox")
VERIFY_RUNS = 3

PATCHABLE = {ACTION_PROPOSE}          # ROT_LOCATOR verdict
COPY_CLASS = "UI_COPY_CHANGE"


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Audit:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.f = open(path, "a")

    def log(self, event: str, **kw):
        row = {"ts": _now(), "event": event}
        row.update(kw)
        self.f.write(json.dumps(row) + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


def _change_for(record, verdict) -> dict | None:
    """Map a labeled failure to a guarded_patch change, or None if the
    failure shape cannot become a safe string swap."""
    loc = record["failure"]["locator"]
    cands = verdict.candidates
    if not cands:
        return None
    new = cands[0]["value"]
    if loc["kind"] == "css" and verdict.klass == "ROT_LOCATOR":
        return {"factory": "locator", "old": loc["value"], "new": new}
    if verdict.klass == COPY_CLASS:
        return {"factory": "get_by_role", "old": loc["value"], "new": new}
    return None


EXTERNAL_CONFTEST = '''"""Sandbox conftest for external targets (v0.5): the suite runs against a
live third-party URL, so base_url comes from the environment, not a local boot."""
import os

import pytest


@pytest.fixture(scope="session")
def base_url():
    return os.environ["PHOENIX_TARGET_URL"]
'''


def verify_sandboxed(patched_source: str, release: str, audit: Audit,
                     filename: str = "test_storefront.py",
                     runs: int = VERIFY_RUNS,
                     conftest_text: str | None = None,
                     extra_env: dict | None = None) -> dict:
    """Three consecutive full-suite runs of the patched file, sandboxed.
    Never touches the live tests/, never trusts its own output text.
    conftest_text: replace the copied demo conftest (external targets point
    base_url at a live URL via extra_env instead of booting demo_app)."""
    SANDBOX_DIR = os.path.join(SANDBOX, filename.replace(".py", ""))
    shutil.rmtree(SANDBOX_DIR, ignore_errors=True)
    os.makedirs(SANDBOX_DIR, exist_ok=True)
    test_copy = os.path.join(SANDBOX_DIR, filename)
    conftest_copy = os.path.join(SANDBOX_DIR, "conftest.py")
    with open(test_copy, "w") as f:
        f.write(patched_source)
    if conftest_text is None:
        shutil.copy(os.path.join(ROOT, "tests", "conftest.py"), conftest_copy)
    else:
        with open(conftest_copy, "w") as f:
            f.write(conftest_text)

    env = {**os.environ, "RELEASE": release, "TEST_PHOENIX_ROOT": ROOT,
           "PYTHONPATH": ROOT}
    if extra_env:
        env.update(extra_env)
    results, logs = [], []
    for i in range(runs):
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", test_copy,
             "-p", "no:cacheprovider"],
            capture_output=True, text=True, env=env, cwd=ROOT, timeout=240)
        tail = (p.stdout or "").strip().splitlines()[-1] if p.stdout else ""
        ok = p.returncode == 0
        results.append({"run": i + 1, "exit": p.returncode, "summary": tail})
        logs.append(f"--- run {i + 1} ({release}, exit {p.returncode}) ---\n"
                    f"{(p.stdout or '')[-1500:]}\n{(p.stderr or '')[-300:]}")
        audit.log("verify_run", run=i + 1, ok=ok, summary=tail)
        if not ok:
            break                                   # consecutive requirement
    return {"all_passed": len(results) == runs and all(r["exit"] == 0 for r in results),
            "runs": results, "log": "\n".join(logs)}


def build_bundle(records, results, patched_source, orig_source, verification,
                 release, rejected_reason=None) -> str | None:
    name = "selfheal-" + dt.datetime.now().strftime("%Y%m%d-%H%M")
    bdir = os.path.join(PRS_DIR, name)
    os.makedirs(bdir, exist_ok=True)
    audit = Audit(os.path.join(bdir, "audit.jsonl"))

    diff = "".join(difflib.unified_diff(
        orig_source.splitlines(keepends=True), patched_source.splitlines(keepends=True),
        fromfile="a/tests/test_storefront.py", tofile="b/tests/test_storefront.py"))
    with open(os.path.join(bdir, "diff.patch"), "w") as f:
        f.write(diff)

    for rec in records:
        with open(os.path.join(bdir, f"evidence_{rec['id']}.json"), "w") as f:
            json.dump({k: rec[k] for k in ("id", "label", "failure", "baseline_html",
                                           "current_html")}, f)

    meta = {
        "created": _now(), "release_under_test": release,
        "applied_to_working_tree": False,
        "patches": [r for r in results if r.get("patched")],
        "bug_reports": [r for r in results if not r.get("patched")],
        "verification": {k: verification[k] for k in ("all_passed", "runs")}
        if verification else None,
        "rejected": rejected_reason,
    }
    with open(os.path.join(bdir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)

    ok = verification and verification["all_passed"]
    lines = [f"# self-heal patch: suite against release {release}", ""]
    lines += ["## Proposed changes", ""]
    for r in results:
        if r.get("patched"):
            flag = "flagged copy change (confirm intentional)" \
                if r["class"] == COPY_CLASS else "class-only locator rot"
            lines.append(f"- `{r['old']}` -> `{r['new']}` in {r['factory']}(): "
                         f"{r['id']}, verdict {r['class']}, {flag}")
            lines.append(f"  - why: {r['rationale']}")
    lines += ["", "## Not patched (bug reports, deliberately)", ""]
    for r in results:
        if not r.get("patched"):
            lines.append(f"- {r['id']}: {r['class']}, action {r['action']}: {r['rationale']}")
    lines += ["", f"## Verification ({VERIFY_RUNS} consecutive full-suite runs, release {release})", ""]
    if verification:
        for run in verification["runs"]:
            lines.append(f"- run {run['run']}: exit {run['exit']}, {run['summary']}")
        lines.append(f"- verdict: {'GREEN' if ok else 'NOT GREEN, do not merge'}")
    lines += ["", "## Evidence",
              f"- failure records and DOM snapshots: evidence_*.json in this folder",
              f"- full logs: verify.log; decisions: audit.jsonl",
              f"- diff: diff.patch", "",
              "## Merge is a human command", "",
              "```",
              "# after reading everything above:",
              "git apply prs/" + name + "/diff.patch",
              "# later, once this repo is on GitHub:",
              "git checkout -b " + name + " && git commit -am 'self-heal: " + release + " selectors' && gh pr create --body-file prs/" + name + "/pr.md",
              "```"]
    with open(os.path.join(bdir, "pr.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    if verification:
        with open(os.path.join(bdir, "verify.log"), "w") as f:
            f.write(verification["log"])
    audit.log("bundle_written", bundle=name, green=bool(ok))
    audit.close()
    return name


def run_pipeline(release="v2", do_verify=True, pw=None):
    orig_source = open(SUITE).read()
    records = capture_all(pw=pw)
    results, changes, bug_reports = [], [], []
    tmp = Audit(os.path.join(PRS_DIR, "pipeline-audit.jsonl"))
    tmp.log("pipeline_start", release=release, records=len(records))

    for rec in records:
        v = classify_step(rec["baseline_html"], rec["current_html"], rec["failure"])
        row = {"id": rec["id"], "class": v.klass, "action": v.action,
               "rationale": v.rationale, "patched": False}
        ch = _change_for(rec, v)
        if ch:
            row["patched"] = True
            row.update(ch)
            changes.append(ch)
        elif v.klass in ("REGRESSION", "UNKNOWN"):
            bug_reports.append(row)
        elif v.klass in (COPY_CLASS,):
            row["patched"] = False       # candidate without positional match
        results.append(row)
        tmp.log("classify", id=rec["id"], klass=v.klass, action=v.action,
                patchable=bool(ch))

    patched_source, rejected = None, None
    if changes:
        try:
            patched_source, _ = guarded_patch(orig_source, changes)
        except PatchRejected as e:
            rejected = str(e)
            tmp.log("guard_reject", reason=rejected)

    verification = None
    if patched_source and do_verify:
        verification = verify_sandboxed(patched_source, release, tmp)
    bundle = build_bundle([r for r in records if r["id"] in
                           {x["id"] for x in results if x.get("patched") or x in bug_reports}],
                          results, patched_source or orig_source, orig_source,
                          verification, release, rejected)
    tmp.log("pipeline_end", bundle=bundle,
            green=bool(verification and verification["all_passed"]))
    tmp.close()
    return {"results": results, "bundle": bundle,
            "verification": verification, "rejected": rejected}


def main():
    out = run_pipeline()
    for r in out["results"]:
        mark = "PATCH" if r["patched"] else "KEEP "
        print(f"{mark} {r['id']:22s} {r['class']:15s} {r['rationale']}")
    if out["rejected"]:
        print(f"GUARD REJECTED ALL CHANGES: {out['rejected']}")
    v = out["verification"]
    if v:
        print(f"verification: {'GREEN' if v['all_passed'] else 'NOT GREEN'} "
              f"({len(v['runs'])}/{VERIFY_RUNS} consecutive runs passed)")
    print(f"bundle: prs/{out['bundle']}/ (nothing applied; merge is the human command)")


if __name__ == "__main__":
    main()
