"""v0.2 failure classifier: is this red test rot, a real regression, or flake?

Design contract, in priority order:
1. REFUSE-BY-DEFAULT. A patch is only proposed when the current page contains
   a structural analog of the dead widget: same tag, same text, same position,
   only class/id changed. No analog means regression, never "unknown,
   let's try patching anyway."
2. The assertion is read-only. If the element the locator lands on EXISTS but
   the value is wrong (text mismatch, wrong count with elements present),
   that is a product bug, full stop. This classifier has no code path that
   proposes changing an expected value.
3. Flaky evidence outranks structure. If an identical re-run passed, the
   failure is timing; do not touch the locator at all.

Input shape (one failing step):
    {
      "locator": {"kind": "css" | "name", "value": ".cart-count" | "Add to cart",
                   "role": "button"},           # kind=name only
      "step": "text" | "count" | "visible",
      "expected": "1",                          # ignored for visible
      "observed": None,                         # what the live probe returned
      "rerun_passed": false,                    # flaky evidence, optional
    }
plus baseline_html (page as recorded when the test last passed) and
current_html (page at failure time).

Output: Verdict(class, action, candidates, rationale). Classes:
    ROT_LOCATOR      css-only rot; candidate new selector, safe to propose
    UI_COPY_CHANGE   visible text changed; candidate new name; flag for human
    REGRESSION       product bug suspected; refuse, never propose
    FLAKY            timing, not structure; no edit
    UNKNOWN          smells like neither; refuse (should be rare by rule 1)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .htmlsig import ElementRecord, find, parse_html

ACTION_PROPOSE = "propose_patch"
ACTION_FLAG = "flag_for_review"
ACTION_REFUSE = "refuse"
ACTION_RERUN = "no_edit_rerun"

ROLES = {"button": {"button"}, "link": {"a"}, "textbox": {"input", "textarea"},
         "checkbox": {"input"}}


@dataclass
class Verdict:
    klass: str
    action: str
    candidates: list = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict:
        return {"class": self.klass, "action": self.action,
                "candidates": self.candidates, "rationale": self.rationale}


def _name_records(records: list[ElementRecord], failure: dict) -> list[ElementRecord]:
    role = failure["locator"].get("role", "")
    want_tags = ROLES.get(role)
    value = failure["locator"]["value"]
    return [r for r in records
            if (r.text == value) and (want_tags is None or r.tag in want_tags)]


def _analog_score(base: ElementRecord, cand: ElementRecord) -> int:
    """Higher is more likely 'the same widget with churned markup'. 0 = no."""
    if cand.tag != base.tag:
        return 0
    score = 1
    if cand.text == base.text:
        score += 4
    else:
        return 0                      # text identity required for a css analog
    if cand.parent_tag == base.parent_tag:
        score += 2
    if cand.sibling_index == base.sibling_index:
        score += 2
    if cand.depth == base.depth:
        score += 1
    return score


def _best_analogs(base: ElementRecord, current: list[ElementRecord]) -> list[ElementRecord]:
    scored = []
    for c in current:
        s = _analog_score(base, c)
        if s:
            scored.append((s, c))
    if not scored:
        return []
    top = max(s for s, _ in scored)
    return [c for s, c in scored if s == top]


def _class_token(rec: ElementRecord) -> str:
    return "." + rec.classes[0] if rec.classes else rec.selector_signature()


def classify_step(baseline_html: str, current_html: str, failure: dict) -> Verdict:
    if failure.get("rerun_passed"):
        return Verdict("FLAKY", ACTION_RERUN, [],
                       "identical re-run passed; timing issue, locator untouched")

    baseline = parse_html(baseline_html)
    current = parse_html(current_html)
    kind = failure["locator"]["kind"]
    step = failure["step"]

    if kind == "name":
        return _classify_name(baseline, current, failure)
    return _classify_css(baseline, current, failure, step)


def _classify_name(baseline, current, failure) -> Verdict:
    """Locator keyed on visible text/role (get_by_role name=, label=)."""
    base_hits = _name_records(baseline, failure)
    cur_hits = _name_records(current, failure)
    value = failure["locator"]["value"]

    if cur_hits:
        # the anchor still exists; whatever failed is content/flow, not the name
        return Verdict("REGRESSION", ACTION_REFUSE, [],
                       f"name {value!r} still present; failure is not locator rot")
    if not base_hits:
        return Verdict("UNKNOWN", ACTION_REFUSE, [],
                       f"name {value!r} not found even in baseline snapshot")

    base = base_hits[0]
    # same widget, different text: copy change. Same tag+parent+position, text differs.
    for c in current:
        if (c.tag == base.tag and c.parent_tag == base.parent_tag
                and c.sibling_index == base.sibling_index and c.text != base.text
                and c.text):
            return Verdict("UI_COPY_CHANGE", ACTION_FLAG,
                           [{"kind": "name", "role": failure["locator"].get("role", ""),
                             "value": c.text}],
                           f"widget at same position now reads {c.text!r} (was {base.text!r}); "
                           "copy change, needs human confirmation it was intentional")
    # no element at that position at all: feature removed or flow changed
    return Verdict("REGRESSION", ACTION_REFUSE, [],
                   f"nothing occupies the position of {value!r} on the current page; "
                   "suspected feature removal or redesign, refuse to patch")


def _classify_css(baseline, current, failure, step) -> Verdict:
    selector = failure["locator"]["value"]
    base_hits = find(baseline, selector)
    cur_hits = find(current, selector)

    if len(cur_hits) == 1 and step == "text":
        got = cur_hits[0].text
        want = (failure.get("expected") or "").strip()
        if got != want:
            return Verdict("REGRESSION", ACTION_REFUSE, [],
                           f"element {selector} exists but shows {got!r}, expected {want!r}; "
                           "value assertion, never patch the expectation")
        return Verdict("UNKNOWN", ACTION_REFUSE, [],
                       "selector resolves and text matches; failure cause is outside this step")

    if len(cur_hits) == 1 and step == "count":
        # selector present but count wrong with exactly one match: multi-element
        # set shrank or grew; that is data, not markup
        return Verdict("REGRESSION", ACTION_REFUSE, [],
                       f"selector {selector} resolves but count expected "
                       f"{failure.get('expected')!r} observed {failure.get('observed')!r}; "
                       "content count changed")

    if cur_hits and step == "visible":
        return Verdict("UNKNOWN", ACTION_REFUSE, [],
                       f"selector {selector} resolves on the failing page")

    # --- selector resolves to nothing (or nothing meaningful) ---
    if not base_hits:
        return Verdict("UNKNOWN", ACTION_REFUSE, [],
                       f"selector {selector} matched nothing even in the baseline snapshot")

    base = base_hits[0]

    if step == "count":
        want = int(failure.get("expected") or 0)
        # candidates: elements matching one of the baseline items' texts, same tag
        cands = []
        seen = set()
        for b in base_hits:
            for c in current:
                key = id(c)
                if key in seen:
                    continue
                if c.tag == b.tag and c.text == b.text and not _same_selector(c, selector):
                    cands.append(c)
                    seen.add(key)
        if len(cands) == want and cands:
            tokens = {_class_token(c) for c in cands}
            if len(tokens) == 1:
                new_sel = tokens.pop()
                return Verdict("ROT_LOCATOR", ACTION_PROPOSE,
                               [{"kind": "css", "value": new_sel}],
                               f"all {want} expected items are present under {new_sel}; "
                               f"class-only change from {selector}")
        return Verdict("REGRESSION", ACTION_REFUSE, [],
                       f"expected {want} items for {selector}, found {len(cands)} "
                       "structural matches; content genuinely missing")

    # step in ("text", "visible") with zero current matches: look for the analog
    analogs = _best_analogs(base, current)
    if analogs:
        tokens = {_class_token(a) for a in analogs}
        cands = [{"kind": "css", "value": t} for t in sorted(tokens)]
        return Verdict("ROT_LOCATOR", ACTION_PROPOSE, cands,
                       f"widget with tag/position/text signature of {selector} found under "
                       f"{', '.join(sorted(tokens))}; class-only change")

    # text expected still somewhere? (element moved and re-styled)
    want = (failure.get("expected") or "").strip()
    if step == "text" and want:
        moved = [c for c in current if c.text == want]
        if moved:
            return Verdict("ROT_LOCATOR", ACTION_FLAG,
                           [{"kind": "css", "value": _class_token(m)} for m in moved[:3]],
                           f"expected text {want!r} exists but not at the widget's position; "
                           "element moved; propose only with human review")

    return Verdict("REGRESSION", ACTION_REFUSE, [],
                   f"no structural analog of {selector} on the current page; "
                   "assume product regression until a human says otherwise")


def _same_selector(rec: ElementRecord, selector: str) -> bool:
    from .htmlsig import matches, parse_selector
    return matches(rec, parse_selector(selector))
