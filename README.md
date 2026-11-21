# test_phoenix

Self-healing end-to-end tests for web apps you did not write.

An agent explores a web app, drafts a Playwright test suite, and runs it in CI. When a selector breaks, the agent patches the test, re-runs to prove the fix, and opens a pull request. A human approves the merge. Passing tests are never touched.

Status: v0.1 through v0.5 are built locally: demo app with churned releases (v1, v2, and v2reg carrying a planted price bug), the 8-test hand-written suite, GitHub Actions config, the failure classifier with its labeled eval, the patch pipeline (guarded AST patching, three-consecutive-run sandbox verification, PR bundles), the cold-start explorer/drafter: BFS + safe form probes + replayable setup flows produce an observed catalog; drafts (rule-based or model-based) pass a no-invented-facts gate, are rendered by a deterministic code generator, and face the same 3x verification; and the breadth proof: the same loop against a third-party public practice app the project does not own or boot, verified 3x green against the live site. The guard refuses assertion edits, navigation retargets, invisible old values, and skip/xfail additions; quote injection stays contained as an inert string. Nothing is applied to the working tree by any pipeline, and nothing is published: merge is a human command.

## Try it

```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -q                    # suite + all evals, 77 passed, 1 skipped (opt-in model test)
RELEASE=v2 .venv/bin/python -m pytest tests -q   # redesign ships: 2 failed, 6 passed
.venv/bin/python -m phoenix.capture              # labeled failures + classifier report
.venv/bin/python -m phoenix.patcher              # self-heal loop: patch, verify, bundle
.venv/bin/python -m phoenix.draft_cli --release v1            # cold-start: explore + rule draft + verify
.venv/bin/python -m phoenix.draft_cli --url https://the-internet.herokuapp.com   # v0.5 breadth: same loop, third-party live site
.venv/bin/python -m phoenix.draft_cli --model openrouter:<model>  # optional: add the model path (needs key; funded and :free OpenRouter routes both answer)
```

The v2 run is the project's thesis in one command: the same suite, unedited, against a markup redesign. Exactly the two class-coupled tests fail (renamed button text, renamed row class). Every label-, role-, and text-based test passes untouched.

The capture run is the v0.2 thesis: five labeled failures, five correct verdicts, printed with the rationale behind each. Two regressions are planted (a wrong price; a removed badge). The classifier refuses both, refuses to emit a candidate patch for either, and the selftest feeds it an unchanged page to prove it does not manufacture rot. The evals catch their own traps: a fixture regex that silently stopped matching (browser HTML re-serializes quotes) produced a rot record mislabeled as regression, and the label mismatch flagged it.

The patcher run is the v0.3 thesis: the three rot/copy failures become a single AST-guarded patch, verified by three consecutive green sandbox runs of the patched suite at v2, and land in prs/<name>/ as a reviewable bundle (diff, DOM evidence, rationale, re-run logs, audit trail, merge commands). The two planted regressions become bug reports and touch no test.

The drafter run is the v0.4 thesis: cold-start from a bare URL, no hand-written knowledge of the app. The explorer finds pages the link graph hides (/checkout is invisible until a cart has an item) and records setup-replayable flows, so every claim in a drafted test traces to something observed. The rule drafter is key-free and the eval reference: a model may only produce steps the rule drafter could have produced from the same observations. Measured history, kept because the failures were the point: a local 9B model's first drafts were rejected 12/12 by the gate (a summed cart total never displayed anywhere, an invented item-list-at-checkout). Two of those rejections turned out to be the HARNESS lying, not the model: inline price text had been recorded glued to product names, and a state-dependent link (Checkout, visible only with a carted item) was never in the page record. Fixed at the source with browser-side leaf-text capture and landing-state folding, each pinned by a regression eval. The next model failure caught was real: a drafted test asserted a hardcoded order reference that changes with run order; the checker let it through and the 3x verification gate failed it, exactly the division of labor: the checker asks "was this ever observed," execution asks "is this actually true every time." After that, a live API model (OpenRouter, free tier) drafted 11 tests that passed the gate AND all three verification runs green; the rule suite stayed green alongside. The model path is opt-in (`--model` / `PHOENIX_LLM=`), via Ollama or any OpenAI-compatible API (OpenAI, OpenRouter) with `response_format: json_object`.


The v0.5 run is the breadth thesis: `draft_cli --url` points the exact same pipeline at an app this repo neither wrote, boots, nor churns. the-internet.herokuapp.com answered 12 pages with zero crashes: an HTTP-auth page came back as an empty shell ("Not authorized", no heading, no controls) and got no test, a custom shadow-DOM form became a probe (its <select> seen and left unfilled by the drafter, which cannot render a fill for it), an action page became a flow. The rule draft went gate-clean and 3x GREEN against the live site, twice independently; the funded gpt-4o-mini path drafted one conservative 81-step home test, also 3x GREEN. Breadth, not depth: the classify/patch half of the loop needs churned releases and stays a demo_app demonstration. The whole external path is mirrored offline, key-free, by a local stand-in app carrying the same nasty shapes (evals/test_v05_external.py), so CI proves it without touching a third party.

## Why this exists

Born from maintaining a Selenium-backed automation platform with hundreds of live selectors against real listing sites. After launch, the number one cost of browser automation is selector rot. Apps change under you daily and suites die by attrition. The standard answer is "write better locators." That only postpones the same failure. test_phoenix automates the maintenance loop itself and gates every automatic patch with verification and human review.

## The loop

```
explore -> draft -> run -> classify -> patch -> verify -> PR
```

1. **Explore:** walk the app via the accessibility tree (ARIA snapshots), not pixels. Enumerate pages, interactive elements, and user flows. Record structure.
2. **Draft:** propose one pytest-playwright test per flow, preferring stable locators (role + accessible name over CSS/XPath chains). A human reviews and merges the first suite. From then on the suite maintains itself.
3. **Run:** headless in CI (GitHub Actions).
4. **Classify the failure:** selector rot vs. app regression vs. flaky wait. This is the hard part and the entire point. A bot that misclassifies a regression as rot will "fix" the test until a real bug reads green.
5. **Patch:** locators and wait handling may change. Assertions and test intent may not. A patch that weakens or deletes an assertion is rejected by a diff checker before review, not by a suggestion.
6. **Verify:** a patch counts only when the previously red test passes three consecutive runs and the rest of the suite stays green.
7. **PR:** the bot opens a pull request carrying the failure evidence, the DOM it patched against, the diff, and the re-run log. A human merges.

## Guardrails

- Patch scope is enforced at the AST level (locator and wait nodes only), not by asking the model politely.
- The agent can never delete or skip a test. Xfail and skip are human-only verbs.
- Every action is logged: input snapshot, patch proposal, re-run results.
- Model output is treated as untrusted input: strict JSON replies, schema-reject on violation.

## Failure modes the evals must catch

- The app genuinely redesigned the flow. The locator patch is right but the test's intent now describes a dead workflow. Eval scenario plants this; expected output is a flagged PR, not a silent green.
- The bot papers over a real regression by loosening a wait. Guard: assertions are read-only. Eval scenario plants this.
- Flaky network wait vs. missing selector. The classifier must separate them. Labeled fixtures ship in the repo.

## Demo target

`demo_app/` ships in this repo: a small FastAPI + HTML storefront whose releases deliberately churn class names, accessible names, and DOM order. That gives the project its proof artifact and its demo. One release renames a button, CI goes red, the bot opens a green patch PR, all on screen in under two minutes.

## Stack

- Python 3.12+
- Playwright for Python, pytest-playwright
- FastAPI (demo app)
- GitHub Actions
- Bring-your-own model interface: any OpenAI-compatible API (OpenAI, OpenRouter) or a local model via Ollama, same pattern as open_otto. The rule drafter and CI harness need no account, no key, no hosted service; the model path is opt-in. OpenRouter is the live-verified backend (free route 2026-11-20, paid route 2026-11-21); `:free` routes work even on an unfunded account.
- pytest

## Roadmap

- [x] v0.1: hand-written pytest-playwright suite for demo_app + GitHub Actions config (suite green on v1 and correctly split on v2, verified locally 2026-11-20; workflow runs on first push)
- [x] v0.2: failure classifier (rot / regression / flaky) against labeled fixtures (5 live + 7 synthetic scenarios, 5/5 live verdicts and all synthetic green, verified locally 2026-11-20; labels true by construction, selftest blocks vacuous passing)
- [x] v0.3: locator-patch agent with re-run gate and PR bot (AST guard refuses assertion/goto/skip edits, invisible old values, and code injection; 3-consecutive-run sandbox verification at the failing release; bundles in prs/ with diff, evidence, rationale, logs; pipeline test asserts the live tree is never touched, verified locally 2026-11-20)
- [x] v0.4: explorer and drafter: cold-start suite from a bare URL (BFS + probes + setup-replayable flows; no-invented-facts gate; deterministic renderer; rule path verified 3x green locally 2026-11-20; live model path verified through OpenRouter 2026-11-20: a free-tier model drafted 11 tests that passed the gate and all three verification runs; getting there caught two real explorer bugs, inline-text gluing and state-hidden links; account state at that date: zero purchased credits, so paid models 402 and only :free routes answer. Post-funding rerun 2026-11-21, paid route openai/gpt-4o-mini: 11 drafted tests, 11/11 gate-clean, 10/11 passed run 1 of execution verification; the failure was a mixed-state claim (empty-cart copy AND a Checkout-link on a bare /cart goto, each individually observed) that the checker's merged per-page records admitted and the 3x run gate rejected; rule suite stayed 7/7 GREEN 3/3)
- [x] v0.5: breadth proof against a third-party public app (the-internet.herokuapp.com: robots-open Saucelabs practice site, never booted or edited by this repo). Cold-start with no demo_app-specific code in the pipeline: browser-style href resolution, auth-walled pages tolerated (skipped or recorded as an empty shell) not fatal, <select> fields probed but never fill-asserted, flow policy pluggable. Rule draft (3 tests: home, shadow-DOM form reject + accept) gate-clean and 3x GREEN against the live site, twice independently; funded-model path (openai/gpt-4o-mini) drafted 1 gate-clean test (an 81-step home enumeration), also 3x GREEN. Verified 2026-11-21. Offline key-free mirror: evals/test_v05_external.py (17 tests) runs the same path against a local stand-in third-party app. Self-heal (classify/patch) stays demo_app-only: it needs releases to churn, which a third-party site does not offer on command

## Non-goals

- Visual / screenshot-diff testing
- Replacing codegen or a test recorder
- Writing business assertions without human review
- Bulk site automation or scraping anything that forbids it
- Team features, permissions, a hosted version

## License

AGPL-3.0 (planned). Free to use, modify, and self-host. Anyone who runs a modified version as a service publishes their changes.
