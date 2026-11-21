"""v0.3 eval: the AST guard must accept exactly the safe edits and refuse
every attack that would turn a red test green without fixing the cause.

These are pure-function tests, no browser, fast. The real end-to-end run
(capture -> classify -> patch -> verify -> bundle) is exercised by
`python -m phoenix.patcher`; the assertions below prove the guard's refusal
paths, which is the part that has to be trustworthy.
"""
import ast

import pytest

from phoenix.astpatch import PatchRejected, guarded_patch

SUITE = open(__file__.replace("evals/test_v03_guard.py",
                              "tests/test_storefront.py")).read()


def test_safe_locator_swap_applies_and_preserves_assertions():
    new, rep = guarded_patch(SUITE, [{"factory": "locator",
                                      "old": ".cart-count", "new": ".cc"}])
    ast.parse(new)                                  # valid syntax
    assert 'locator(".cc")' in new
    assert 'to_have_text("1")' in new               # assertion untouched
    assert rep["sites"] >= 1


def test_comments_survive_the_splice():
    new, _ = guarded_patch(SUITE, [{"factory": "locator",
                                    "old": ".cart-line", "new": ".c-row"}])
    assert "# ROT: class selector" in new           # byte-preserved, not reparsed


def test_refuse_change_to_assertion_value():
    # attacker wants the expected text to match a broken app
    with pytest.raises(PatchRejected) as e:
        guarded_patch(SUITE, [{"factory": "to_have_text",
                               "old": "1", "new": "0"}])
    assert "not a locator" in str(e.value).lower()


def test_refuse_goto_retarget():
    with pytest.raises(PatchRejected):
        guarded_patch(SUITE, [{"factory": "goto",
                               "old": "/product/CK-2L", "new": "/"}])


def test_refuse_invisible_old_value():
    with pytest.raises(PatchRejected) as e:
        guarded_patch(SUITE, [{"factory": "locator",
                               "old": ".does-not-exist", "new": ".cc"}])
    assert "refusing to guess" in str(e.value).lower()


def test_quote_injection_stays_inert_string():
    # candidates are serialized with json.dumps, so quotes/newlines cannot
    # escape the string literal: the worst case is an ugly selector, not code.
    payload = '.cc"); request.state.x=1; ("'
    new, _ = guarded_patch(SUITE, [{"factory": "locator", "old": ".cart-count",
                                    "new": payload}])
    tree = ast.parse(new)                     # still valid, still structured
    consts = [n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert payload in consts                  # contained as ONE string value
    assert "request.state" not in "".join(
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name))
    names = lambda s: sorted(n.name for n in ast.walk(ast.parse(s))
                             if isinstance(n, ast.FunctionDef))
    assert names(new) == names(SUITE)


def test_refuse_newline_injection():
    with pytest.raises(PatchRejected):
        guarded_patch(SUITE, [{"factory": "locator", "old": ".cart-count",
                               "new": '.cc"\nimport os'}])


def test_refuse_empty_or_noop_change():
    with pytest.raises(PatchRejected):
        guarded_patch(SUITE, [{"factory": "locator", "old": ".cart-count", "new": "  "}])
    with pytest.raises(PatchRejected):
        guarded_patch(SUITE, [{"factory": "locator", "old": ".cc", "new": ".cc"}])


def test_function_set_can_never_change():
    """Even a syntactically valid swap that would delete or rename a test is
    impossible through this API (only locator strings are touched), and the
    post-check enforces the function set. Assert both properties here."""
    new, _ = guarded_patch(SUITE, [{"factory": "locator",
                                    "old": ".cart-line", "new": ".c-row"}])
    names = lambda s: sorted(n.name for n in ast.walk(ast.parse(s))
                             if isinstance(n, ast.FunctionDef))
    assert names(new) == names(SUITE)


def test_legal_patch_cannot_touch_test_count_or_markers():
    """The splice API only rewrites string constants inside factory calls, so
    adding/removing a test or a skip/xfail marker is inexpressible through it.
    The post-check enforces this even against a guard bug. This test applies
    the full three-change patch and asserts the invariants hold."""
    new, _ = guarded_patch(SUITE, [
        {"factory": "locator", "old": ".cart-count", "new": ".cc"},
        {"factory": "locator", "old": ".cart-line", "new": ".c-row"},
        {"factory": "get_by_role", "old": "Add to cart", "new": "Add item"},
    ])
    assert new.count("def test_") == SUITE.count("def test_")
    assert new.count("skip") == SUITE.count("skip")
    assert new.count("xfail") == SUITE.count("xfail")
    # the three expected selector swaps happened
    assert new.count(".cc") > SUITE.count(".cc")
    assert new.count(".c-row") > SUITE.count(".c-row")
    assert new.count("Add item") > SUITE.count("Add item")


def test_real_suite_protected_constants_are_read_only():
    """A patch touching ONLY protected strings (expectation args) must be
    impossible: there is no locator string equal to a to_have_text argument
    in the suite, so the guard refuses to guess."""
    with pytest.raises(PatchRejected):
        guarded_patch(SUITE, [{"factory": "locator", "old": "$72.00",
                               "new": "$0.00"}])
