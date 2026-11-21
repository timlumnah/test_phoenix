"""AST-guarded test patching: the enforcement half of "assertions are
read-only."

Only string constants that sit inside locator/wait factory calls may change,
and only when they equal the declared old value exactly. Everything else in
the file must survive byte-identical in spirit: function names, assertion
arguments (every expect(...).to_* argument), goto URLs, skip/xfail markers,
and valid syntax. If a post-check disagrees with the splice, the patch is
rejected and the original file stands. The model never edits files; it
proposes selector strings and this module decides whether they can land.

Note: constants are spliced by ast position, not by regex or unparsing,
so comments and formatting outside the replaced literals are untouched.
"""
from __future__ import annotations

import ast
import json

FACTORIES = {
    "locator", "get_by_role", "get_by_label", "get_by_text",
    "get_by_placeholder", "wait_for_selector", "query_selector",
}
TO_PREFIX = "to_"          # expect(...).to_have_text / to_have_count / ...
NAV_METHODS = {"goto", "reload"}


class PatchRejected(Exception):
    pass


def _calls(tree):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _func_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _string_consts(node) -> list[ast.Constant]:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n)
    return out


def _protected_constants(tree) -> list[str]:
    """Multiset of every string constant in a position the patcher may never
    touch: expectation arguments and navigation URLs. Only args/keywords are
    protected; the receiver subtree of a to_* call (the expect(...) part) is
    where legitimate locator constants live."""
    vals = []
    for call in _calls(tree):
        name = _func_name(call)
        if name.startswith(TO_PREFIX) or name in NAV_METHODS:
            inner = list(call.args) + list(call.keywords)
            for node in inner:
                vals.extend(c.value for c in _string_consts(node))
    return sorted(vals)


def _patchable_constants(tree, factory: str, old: str):
    """Constant nodes equal to `old` that sit directly inside a call to
    `factory` (positional args or keyword values)."""
    hits = []
    for call in _calls(tree):
        if _func_name(call) != factory:
            continue
        inner = list(call.args) + list(call.keywords)
        for node in inner:
            for c in _string_consts(node):
                if c.value == old:
                    hits.append(c)
    return hits


def _splice(source: str, replacements) -> str:
    """Replace (lineno, col_offset, end_lineno, end_col_offset, new_text)
    spans, last first so offsets stay valid."""
    lines = source.splitlines(keepends=True)
    # absolute offsets
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln))

    def abs_pos(lineno, col):
        return starts[lineno - 1] + col

    text = source
    for (l1, c1, l2, c2, new) in sorted(replacements, reverse=True):
        a, b = abs_pos(l1, c1), abs_pos(l2, c2)
        text = text[:a] + new + text[b:]
    return text


def guarded_patch(source: str, changes: list[dict]) -> tuple[str, dict]:
    """changes: [{"factory": "locator", "old": ".cart-count", "new": ".cc"}, ...]
    Returns (patched_source, report). Raises PatchRejected on any violation."""
    if not changes:
        raise PatchRejected("empty change list")
    for ch in changes:
        if ch["factory"] not in FACTORIES:
            raise PatchRejected(f"factory {ch['factory']!r} is not a locator/wait call")
        for k in ("old", "new"):
            if not isinstance(ch[k], str) or not ch[k].strip():
                raise PatchRejected("old/new must be non-empty strings")
        if ch["old"] == ch["new"]:
            raise PatchRejected("old equals new")
        if any(q in ch["new"] for q in ('\n', '\\n', '"""')):
            raise PatchRejected("new value looks like injected code/multiline")

    tree = ast.parse(source)
    fn_before = sorted(n.name for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    prot_before = _protected_constants(tree)

    replacements, report_rows = [], []
    for ch in changes:
        hits = _patchable_constants(tree, ch["factory"], ch["old"])
        if not hits:
            raise PatchRejected(
                f"no string {ch['old']!r} found inside {ch['factory']}() calls; "
                "refusing to guess where it lives")
        new_repr = json.dumps(ch["new"])     # safe quoting, double style
        for c in hits:
            replacements.append((c.lineno, c.col_offset,
                                 c.end_lineno, c.end_col_offset, new_repr))
        report_rows.append({"factory": ch["factory"], "old": ch["old"],
                            "new": ch["new"], "sites": len(hits)})

    patched = _splice(source, replacements)

    # ---- post-checks: the splice may not have done what it promised ----
    try:
        ptree = ast.parse(patched)
    except SyntaxError as e:
        raise PatchRejected(f"patched source does not parse: {e}")
    fn_after = sorted(n.name for n in ast.walk(ptree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    if fn_after != fn_before:
        raise PatchRejected("function set changed; deletions and additions are forbidden")
    if _protected_constants(ptree) != prot_before:
        raise PatchRejected("an expectation argument or navigation URL moved; "
                            "assertions are read-only")
    skip_before = sum(1 for n in ast.walk(tree)
                      if isinstance(n, ast.Attribute) and n.attr in ("skip", "xfail"))
    skip_after = sum(1 for n in ast.walk(ptree)
                     if isinstance(n, ast.Attribute) and n.attr in ("skip", "xfail"))
    if skip_after != skip_before:
        raise PatchRejected("skip/xfail markers moved; those are human-only verbs")

    report = {"changes": report_rows, "sites": len(replacements)}
    return patched, report
