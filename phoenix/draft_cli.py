"""CLI: cold-start draft pipeline. explore -> draft (rule + optional model)
-> gate -> render -> sandbox verify -> drafts/<ts>/ report.

Run:  python -m phoenix.draft [--model ollama:qwen3.5:9b] [--release v1]
      python -m phoenix.draft --url https://the-internet.herokuapp.com   # v0.5
Exit 0 iff the rule-drafted suite verifies green; model drafts are reported
(gate violations and verification) but never block the rule result: the
fallback is the product, the model is an amplifier.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from urllib.parse import urlparse

from phoenix.draft import check_draft, llm_draft, render, rule_draft
from phoenix.explore import _action_page_policy, explore
from phoenix.model import ModelError, get_model
from phoenix.patcher import EXTERNAL_CONFTEST, Audit, verify_sandboxed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None,
                    help="e.g. ollama:qwen3.5:9b; omit for rule-only")
    ap.add_argument("--release", default="v1")
    ap.add_argument("--url", default=None,
                    help="v0.5 breadth proof: explore + verify against a live "
                         "external URL instead of the local demo release")
    args = ap.parse_args()

    ts = dt.datetime.now().strftime("%Y%m%d-%H%M")
    target = f"external-{args.url.split('//', 1)[-1]}" if args.url else args.release
    outdir = os.path.join(ROOT, "drafts", ts)
    os.makedirs(outdir, exist_ok=True)
    audit = Audit(os.path.join(outdir, "audit.jsonl"))
    report = {"target": target, "ts": ts}
    if args.url:
        report["url"] = args.url

    print(f"[draft] exploring {target} ...", flush=True)
    if args.url:
        catalog = explore(args.url, flow_policy=_action_page_policy)
    else:
        from phoenix.explore import explore_release
        catalog = explore_release(args.release)
    json.dump(catalog, open(os.path.join(outdir, "catalog.json"), "w"), indent=1)
    report["pages"] = sorted(catalog["pages"])
    report["probes"] = len(catalog["probes"])
    report["flows"] = len(catalog["flow"])
    if args.url:
        report["skipped_pages"] = catalog.get("skipped", [])
        report["probe_errors"] = catalog.get("probe_errors", [])
    audit.log("explored", pages=len(catalog["pages"]), probes=report["probes"])
    print(f"[draft] observed {len(catalog['pages'])} pages, "
          f"{report['probes']} form probes, {report['flows']} add flows",
          flush=True)

    for source_name, draft in _drafts(catalog, args.model, audit, report):
        entry = report.setdefault(source_name, {})
        violations = check_draft(catalog, draft)
        entry["tests"] = len(draft.get("tests", []))
        entry["gate_violations"] = violations
        json.dump(draft, open(os.path.join(outdir, f"draft_{source_name}.json"), "w"),
                  indent=1)
        if violations:
            entry["verified"] = None
            print(f"[draft] {source_name}: {entry['tests']} tests drafted, "
                  f"REJECTED by gate ({len(violations)} violations)", flush=True)
            for vline in violations[:8]:
                print(f"    - {vline}")
            continue
        source = render(draft)
        suite_path = os.path.join(outdir, f"draft_suite_{source_name}.py")
        with open(suite_path, "w") as f:
            f.write(source)
        ver = verify_sandboxed(
            source, args.release, audit,
            filename=f"draft_test_{source_name}.py",
            conftest_text=EXTERNAL_CONFTEST if args.url else None,
            extra_env={"PHOENIX_TARGET_URL": _origin(args.url)} if args.url else None)
        entry["verified"] = ver["all_passed"]
        entry["runs"] = ver["runs"]
        with open(os.path.join(outdir, f"verify_{source_name}.log"), "w") as f:
            f.write(ver["log"])
        print(f"[draft] {source_name}: {entry['tests']} tests, gate clean, "
              f"verification {'GREEN 3/3' if ver['all_passed'] else 'NOT GREEN'}",
              flush=True)

    json.dump(report, open(os.path.join(outdir, "report.json"), "w"), indent=1)
    audit.close()
    ok = report.get("rule", {}).get("verified") is True and not report.get(
        "rule", {}).get("gate_violations")
    print(f"[draft] report: {outdir}/report.json")
    sys.exit(0 if ok else 1)


def _drafts(catalog, model_spec, audit, report):
    yield "rule", rule_draft(catalog)
    if model_spec:
        try:
            model = get_model(model_spec)
            print(f"[draft] asking {model.name} (one shot) ...", flush=True)
            draft = llm_draft(catalog, model)
            audit.log("model_draft", model=model_spec,
                      tests=len(draft.get("tests", [])))
            yield "model", draft
        except Exception as e:
            report["model_error"] = str(e)
            print(f"[draft] model path failed, rule result stands: {e}", flush=True)


if __name__ == "__main__":
    main()
