"""Drafter: turn the observed catalog into a test suite. Two sources, one gate.

Sources:
  rule_draft  deterministic, key-free. Emits exactly what the catalog
              supports. Also the eval reference: any model draft is compared
              to it. A model may only ever produce steps the rule drafter
              could have produced from the same observations.
  llm_draft   the model sees the catalog JSON and the step vocabulary and
              returns a draft in strict JSON. The model never writes code.

Gate (check_draft), all in code:
  - schema: allowed step kinds, valid names, unique test names
  - no invented facts: every assertion string must appear in the
    observables of a page the catalog actually recorded for this flow
    (page, form probe results, add-flow landing pages)
  - every click target must be a button text recorded on the page being
    visited
  - inputs (fill values) are requests, not claims: any literal allowed,
    but the label must exist

Verification (verify_draft, in draft pipeline / CLI): render, run 3x in
sandbox at the release explored, only green drafts are kept.
"""
from __future__ import annotations

import json
import re

STEP_KINDS = {"goto", "click_button_named", "click_link_named", "fill_label",
              "expect_heading", "expect_text", "expect_button_named",
              "expect_link_named", "expect_label"}
NAME_RE = re.compile(r"^test_[a-z0-9_]{2,60}$")


class DraftError(Exception):
    pass


# --- observables universe ---------------------------------------------------

def observed_strings(catalog: dict) -> dict[str, set]:
    """path -> every string that page is OBSERVED to show (its own copy plus
    the copy seen after its forms' probes and its add-flow clicks)."""
    uni: dict[str, set] = {p: _obs_set(o) for p, o in catalog["pages"].items()}
    for key, pr in catalog.get("probes", {}).items():
        src = pr["page"]
        for res in ("empty_result", "valid_result"):
            uni.setdefault(src, set()).update(_obs_set(pr[res]["obs"]))
    for src, fl in catalog.get("flow", {}).items():
        uni.setdefault(src, set()).update(_obs_set(fl["landing_page"]))
        uni.setdefault(src, set()).add(fl["clicked_button"])
        uni.setdefault(fl["lands_on"], set()).update(_obs_set(fl["landing_page"]))
    return uni


def _obs_set(obs: dict) -> set:
    s = set()
    if obs.get("h1"):
        s.add(obs["h1"])
    for k in ("buttons", "links", "labels", "main_texts"):
        s.update(obs.get(k, []))
    return s


def _obs_fields(obs: dict) -> dict[str, set]:
    return {"h1": {obs.get("h1", "")} - {""},
            "buttons": set(obs.get("buttons", [])),
            "links": set(obs.get("links", [])),
            "labels": set(obs.get("labels", [])),
            "selects": set(obs.get("select_labels", [])),
            "texts": set(obs.get("main_texts", []))}


def _group(catalog, path: str) -> dict[str, set]:
    """Everything OBSERVED for a page: its own render, the results of its
    form probes, and the landings of add-flows that START on it."""
    g = _obs_fields(catalog["pages"].get(path, {}))
    for pr in catalog.get("probes", {}).values():
        if pr["page"] == path:
            for res in ("empty_result", "valid_result"):
                other = _obs_fields(pr[res]["obs"])
                for k in g:
                    g[k] |= other[k]
    for src, fl in catalog.get("flow", {}).items():
        if src == path:
            other = _obs_fields(fl["landing_page"])
            for k in g:
                g[k] |= other[k]
    return g


# --- the gate ----------------------------------------------------------------

def check_draft(catalog: dict, draft: dict) -> list[str]:
    """Returns a list of violations (empty = clean). Never raises on model
    junk; the caller rejects on nonempty.

    Context tracks where the TEST would actually be: a goto sets the page; a
    click transitions ONLY along recorded, observed flows (button -> that
    flow's landing page; link -> that link's recorded href). A model may
    reason about navigation, but only where the explorer SAW it go."""
    v = []
    if not isinstance(draft, dict) or not isinstance(draft.get("tests"), list):
        return ["draft is not {'tests': [...]}"]
    seen_names = set()
    for t in draft["tests"]:
        name = t.get("name", "")
        if not isinstance(name, str) or not NAME_RE.match(name or ""):
            v.append(f"bad test name {name!r}")
        elif name in seen_names:
            v.append(f"duplicate test name {name!r}")
        seen_names.add(name)
        ctx = []            # stack of page paths currently plausible
        for i, step in enumerate(t.get("steps", [])):
            if not isinstance(step, dict) or len(step) != 1 or next(iter(step)) not in STEP_KINDS:
                v.append(f"{name} step {i}: unknown step kind")
                continue
            kind = next(iter(step))
            arg = step[kind]
            fields = lambda: _merged(catalog, ctx)
            if kind == "goto":
                if arg not in catalog["pages"]:
                    v.append(f"{name} step {i}: goto unobserved path {arg!r}")
                    ctx = []
                else:
                    ctx = [arg]
            elif kind in ("click_button_named", "click_link_named"):
                if not isinstance(arg, str):
                    v.append(f"{name} step {i}: click needs a string")
                    continue
                f = fields()
                if kind == "click_button_named":
                    if arg not in f["buttons"]:
                        v.append(f"{name} step {i}: button {arg!r} not observed "
                                 "in current context")
                        continue
                    dest = _flow_dest(catalog, ctx, arg)
                    if dest:
                        ctx = [dest] + ctx        # landing page now in view
                else:
                    if arg not in f["links"]:
                        v.append(f"{name} step {i}: link {arg!r} not observed "
                                 "in current context")
                        continue
                    dest = _link_dest(catalog, ctx, arg)
                    if dest:
                        ctx = [dest] + ctx
            elif kind == "fill_label":
                lab, value = (arg if isinstance(arg, list) and len(arg) == 2
                              else (None, None))
                if lab is None or not isinstance(value, str):
                    v.append(f"{name} step {i}: fill_label needs [label, value]")
                elif lab not in fields()["labels"]:
                    v.append(f"{name} step {i}: label {lab!r} not observed "
                             "in current context")
                elif lab in fields()["selects"]:
                    # v0.5: the renderer drives fills with fill(), which
                    # cannot operate a <select>. The rule drafter never
                    # emits such a step, so this is also the ceiling rule.
                    v.append(f"{name} step {i}: label {lab!r} is a <select> "
                             "field; fill cannot drive it")
            else:
                want = {"expect_heading": "h1", "expect_text": "texts",
                        "expect_button_named": "buttons",
                        "expect_link_named": "links",
                        "expect_label": "labels"}[kind]
                if not isinstance(arg, str) or not arg:
                    v.append(f"{name} step {i}: {kind} needs a non-empty string")
                elif not ctx:
                    v.append(f"{name} step {i}: assertion before any goto")
                elif arg not in fields()[want]:
                    v.append(f"{name} step {i}: {kind} {arg!r} was never "
                             "observed in the current context (invented fact?)")
    return v


def _merged(catalog, ctx: list[str]) -> dict[str, set]:
    out = {"h1": set(), "buttons": set(), "links": set(), "labels": set(),
           "selects": set(), "texts": set()}
    for p in ctx:
        g = _group(catalog, p)
        for k in out:
            out[k] |= g[k]
    return out


def _flow_dest(catalog, ctx: list[str], button: str) -> str | None:
    for src, fl in catalog.get("flow", {}).items():
        if src in ctx and fl["clicked_button"] == button:
            return fl["lands_on"]
    return None


def _link_dest(catalog, ctx: list[str], text: str) -> str | None:
    for p in ctx:
        for pair in catalog["pages"].get(p, {}).get("link_pairs", []):
            if pair[0] == text:
                return pair[1]
    return None


# --- rule drafter: the ceiling every model draft must fit under ---------------

def rule_draft(catalog: dict) -> dict:
    tests = []
    home = catalog["pages"].get("/", {})
    if home.get("h1"):
        steps = [{"goto": "/"}, {"expect_heading": home["h1"]}]
        steps += [{"expect_link_named": t} for t in home.get("links", [])]
        steps += [{"expect_text": t} for t in _prices(home)]
        tests.append({"name": "test_home_shows_catalog", "steps": steps})
    for path in sorted(catalog.get("flow", {})):
        # v0.5: driven by recorded flows, not by a /product/ path pattern,
        # so external targets with their own flow policy get flow tests.
        obs = catalog["pages"].get(path)
        fl = catalog["flow"][path]
        if not obs or not obs.get("buttons") or not obs.get("h1"):
            continue
        steps = [{"goto": path}, {"expect_heading": obs["h1"]},
                 {"click_button_named": fl["clicked_button"]},
                 {"expect_text": obs["h1"]}]
        for pr in _prices(obs):
            steps.append({"expect_text": pr})
        tests.append({"name": f"test_flow_from_{re.sub(r'[^a-z0-9]', '_', path.lower())}_lands",
                      "steps": steps})
    # form tests: group probe variants by (page, form index); prefer a
    # setup-replayed variant for the success path, plain for the empty path
    variants: dict[str, list[dict]] = {}
    for key, pr in catalog.get("probes", {}).items():
        base = key.split("@")[0]
        variants.setdefault(base, []).append(pr)
    for base in sorted(variants):
        plist = sorted(variants[base], key=lambda p: (p.get("setup") or "",))
        plain = next((p for p in plist if not p.get("setup")), plist[0])
        success = next((p for p in reversed(plist) if p.get("setup")), None) or \
            (plain if plain["valid_result"]["path"] != plain["page"] else None)
        path, btn = plain["page"], plain["submit_button"]
        labels = catalog["pages"].get(path, {}).get("labels", [])
        if not labels:
            continue   # a form with no fields (buy button) is not a
                       # validation surface; its flow tests live in the
                       # per-product tests instead
        slug = re.sub(r'[^a-z0-9]', '_', path.lower())
        errs = _new_texts(plain["empty_result"]["obs"],
                          catalog["pages"].get(path, {}))
        if errs:
            tests.append({"name": f"test_{slug}_rejects_empty_form",
                          "steps": [{"goto": path}, {"click_button_named": btn}] +
                                   [{"expect_text": e} for e in errs]})
        if success:
            steps = []
            setup = success.get("setup")
            if setup:
                fl = catalog.get("flow", {}).get(setup, {})
                cb = fl.get("clicked_button") or (
                    catalog["pages"].get(setup, {}).get("buttons") or [None])[0]
                if cb:
                    steps += [{"goto": setup}, {"click_button_named": cb}]
            steps += [{"goto": path}]
            group_sel = _group(catalog, path)["selects"]
            for lab in catalog["pages"].get(path, {}).get("labels", []):
                if lab in group_sel:
                    continue   # <select>: fill cannot drive it (see gate)
                steps.append({"fill_label": [lab, "test@example.com" if "email" in lab.lower()
                                             else "Test Person"]})
            steps += [{"click_button_named": btn}]
            ok_h1 = success["valid_result"]["obs"].get("h1", "")
            ok_texts = _new_texts(success["valid_result"]["obs"],
                                  catalog["pages"].get(path, {}))
            if ok_h1:
                steps.append({"expect_heading": ok_h1})
            elif ok_texts:
                steps += [{"expect_text": t} for t in ok_texts]
            else:
                continue   # success with no observable copy is not testable
            tests.append({"name": f"test_{slug}_accepts_valid_form", "steps": steps})
    return {"tests": tests}


def _prices(obs: dict) -> list[str]:
    return [t for t in obs.get("main_texts", []) if re.fullmatch(r"\$\d+(\.\d\d)?", t)]


def _new_texts(after: dict, before_page: dict) -> list[str]:
    """Copy that appeared after the empty submit and is nowhere on the form
    page itself: the app's own complaint about missing input."""
    before = _obs_set(before_page)
    return [t for t in after.get("main_texts", []) if t not in before][:4]


# --- llm drafter ---------------------------------------------------------------

PROMPT_SYSTEM = (
    "You draft Playwright tests as JSON steps for an app you explored. "
    "You may only state what the OBSERVED catalog shows for the page under "
    "test, including after its form probes and add-to-cart clicks. "
    "Never invent copy, prices, headings, or routes. Output ONLY JSON: "
    '{"tests": [{"name": "test_...", "steps": [...]}]}, max 12 tests. '
    "Allowed steps: {\"goto\": path}, {\"click_button_named\": text}, "
    "{\"click_link_named\": text}, "
    "{\"fill_label\": [label, value]}, {\"expect_heading\": text}, "
    "{\"expect_text\": text}, {\"expect_button_named\": text}, "
    "{\"expect_link_named\": text}, {\"expect_label\": text}. "
    "Prefer role/label/text steps; no CSS. "
    "Never assert order-dependent or generated values (order references, "
    "counters, anything containing a number that could change between runs): "
    "assert only stable copy from the catalog. "
    "Fields whose label appears in a page's select_labels are dropdowns: "
    "fill_label cannot drive them, never emit fill_label for those labels."
)


def llm_draft(catalog: dict, model) -> dict:
    user = "OBSERVED CATALOG:\n" + json.dumps(catalog)
    raw = model.complete_json(PROMPT_SYSTEM, user)
    try:
        draft = json.loads(raw)
    except json.JSONDecodeError as e:
        raise DraftError(f"model reply is not JSON: {e}")
    return draft


# --- renderer: steps to code, deterministic template, no model hands on source --

HEADER = (
    '"""DRAFTED suite: generated by test_phoenix from observed catalog. '
    'Human review gate: this file runs in a sandbox; it is merged by a person."""\n'
    "from playwright.sync_api import Page, expect\n"
)


def render(draft: dict) -> str:
    out = [HEADER]
    for t in draft["tests"]:
        out.append(f"\n\ndef {t['name']}(page: Page):")
        if not t["steps"]:
            out.append("    pass")
        for step in t["steps"]:
            kind = next(iter(step))
            arg = step[kind]
            q = json.dumps(arg, ensure_ascii=True)
            if kind == "goto":
                out.append(f"    page.goto({q})")
            elif kind == "click_button_named":
                out.append(f"    page.get_by_role(\"button\", name={q}, exact=True).click()")
            elif kind == "click_link_named":
                out.append(f"    page.get_by_role(\"link\", name={q}, exact=True).click()")
            elif kind == "fill_label":
                out.append(f"    page.get_by_label({json.dumps(arg[0])}, exact=True)"
                           f".fill({json.dumps(arg[1])})")
            elif kind == "expect_heading":
                out.append(f"    expect(page.get_by_role(\"heading\", name={q},"
                           f" exact=True).first).to_be_visible()")
            elif kind == "expect_text":
                # exact=False on purpose: the catalog stores an element's OWN
                # direct text ("Total:") which Playwright's exact match could
                # never address; a substring-visible assertion is exactly the
                # claim "this text is on screen"
                out.append(f"    expect(page.get_by_text({q}, exact=False).first)"
                           f".to_be_visible()")
            elif kind == "expect_button_named":
                out.append(f"    expect(page.get_by_role(\"button\", name={q},"
                           f" exact=True).first).to_be_visible()")
            elif kind == "expect_link_named":
                out.append(f"    expect(page.get_by_role(\"link\", name={q},"
                           f" exact=True).first).to_be_visible()")
            elif kind == "expect_label":
                out.append(f"    expect(page.get_by_label({q}, exact=True).first)"
                           f".to_be_visible()")
    return "\n".join(out) + "\n"
