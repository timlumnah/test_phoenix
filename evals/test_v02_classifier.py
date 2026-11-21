"""v0.2 eval: the classifier must produce the labeled verdict for every
scenario, and must NEVER propose a patch on a regression. Labels are true by
construction (planted churn, planted bug), so a failure here is a classifier
bug, not a fixture problem.

Two data sources:
  live      - real browser, real demo app at two releases (phoenix.capture)
  synthetic - hand-written html pairs for cases the live app cannot express
              (row deletion, button removal, flaky evidence)

Run from repo root: python -m pytest evals -q
"""
import pytest

from phoenix.classify import (ACTION_FLAG, ACTION_PROPOSE, ACTION_REFUSE,
                              classify_step)

# --- synthetic fixtures: minimal, hand-written, readable ------------------

BASE_PRODUCT = """<html><body><header><a href='/'>Phoenix</a></header>
<main><h1>Copper Kettle, 2L</h1><p class='p-cost'>$48.00</p>
<form method='post'><button type='submit'>Add to cart</button></form></main>
</body></html>"""

CUR_BUTTON_RENAMED = """<html><body><header><a href='/'>Phoenix</a></header>
<main><h1>Copper Kettle, 2L</h1><p class='p-cost'>$48.00</p>
<form method='post'><button type='submit'>Add item</button></form></main>
</body></html>"""

CUR_BUTTON_GONE = """<html><body><header><a href='/'>Phoenix</a></header>
<main><h1>Copper Kettle, 2L</h1><p class='p-cost'>$48.00</p>
<form method='post'><input type='hidden' name='sku'></form></main>
</body></html>"""

BASE_CART = """<html><body><header><span class='cart-count'>2</span></header>
<main><h1>Cart</h1><table><tbody>
<tr class='cart-line'><td>Copper Kettle, 2L $48.00</td></tr>
<tr class='cart-line'><td>Hemp Canvas Tote $24.00</td></tr>
</tbody></table><p>Total: <span class='cart-total'>$72.00</span></p></main>
</body></html>"""

CUR_ROWS_GONE = """<html><body><header><span class='cc'>2</span></header>
<main><h1>Cart</h1><p>Cart is empty.</p></main></body></html>"""

CUR_ONE_ROW = """<html><body><header><span class='cart-count'>2</span></header>
<main><h1>Cart</h1><table><tbody>
<tr class='cart-line'><td>Copper Kettle, 2L $48.00</td></tr>
</tbody></table></main></body></html>"""

CUR_MOVED_BADGE = """<html><body><header><span class='cc'>2</span></header>
<main><h1>Cart</h1></main><footer><span class='badge-x'>2</span></footer>
</body></html>"""


def f_css(sel, step, expected, observed=None, rerun=False):
    return {"locator": {"kind": "css", "value": sel}, "step": step,
            "expected": expected, "observed": observed, "rerun_passed": rerun}


def f_name(value, role="button", rerun=False):
    return {"locator": {"kind": "name", "value": value, "role": role},
            "step": "visible", "expected": None, "observed": False,
            "rerun_passed": rerun}


SYNTHETIC = [
    ("copy_change_flagged", BASE_PRODUCT, CUR_BUTTON_RENAMED, f_name("Add to cart"),
     "UI_COPY_CHANGE", ACTION_FLAG),
    ("button_removed_refuse", BASE_PRODUCT, CUR_BUTTON_GONE, f_name("Add to cart"),
     "REGRESSION", ACTION_REFUSE),
    ("rows_deleted_refuse", BASE_CART, CUR_ROWS_GONE, f_css(".cart-line", "count", 2, 0),
     "REGRESSION", ACTION_REFUSE),
    ("row_count_shrank_refuse", BASE_CART, CUR_ONE_ROW,
     f_css(".cart-line", "count", 2, 1), "REGRESSION", ACTION_REFUSE),
    ("badge_moved_still_rot", BASE_CART, CUR_MOVED_BADGE,
     f_css(".cart-count", "text", "2", None), "ROT_LOCATOR", ACTION_PROPOSE),
    ("flaky_no_edit", BASE_CART, CUR_ROWS_GONE,
     f_css(".cart-line", "count", 2, 0, rerun=True), "FLAKY", "no_edit_rerun"),
]


@pytest.mark.parametrize("case", SYNTHETIC, ids=[c[0] for c in SYNTHETIC])
def test_synthetic_scenarios(case):
    _, base, cur, failure, want_class, want_action = case
    v = classify_step(base, cur, failure)
    assert v.klass == want_class, v.rationale
    assert v.action == want_action, v.rationale


def test_price_wrong_is_regression_never_propose():
    v = classify_step(BASE_PRODUCT,
                      BASE_PRODUCT.replace("$48.00", "$60.50"),
                      f_css(".p-cost", "text", "$48.00", "$60.50"))
    assert v.klass == "REGRESSION"
    assert v.action == ACTION_REFUSE
    assert not v.candidates


# --- live labeled data ------------------------------------------------------

@pytest.fixture(scope="module")
def live_records(playwright):
    """Shares pytest-playwright's sync instance: two sync_playwright objects
    in one process collide ("Sync API inside the asyncio loop"), and
    collection order decides which gets there first. Shared is always safe."""
    from phoenix.capture import capture_all
    return capture_all(pw=playwright)


def test_live_classifications_match_labels(live_records):
    bad = []
    for rec in live_records:
        v = classify_step(rec["baseline_html"], rec["current_html"], rec["failure"])
        if v.klass != rec["label"]:
            bad.append(f"{rec['id']}: label={rec['label']} got={v.klass} ({v.rationale})")
    assert not bad, "live eval failures:\n" + "\n".join(bad)


def test_live_regressions_are_never_patchable(live_records):
    """The safety property. A regression verdict that carries a proposed
    patch would let v0.3 'fix' real bugs green. Forbidden."""
    for rec in live_records:
        if rec["label"] == "REGRESSION":
            v = classify_step(rec["baseline_html"], rec["current_html"], rec["failure"])
            assert v.action != ACTION_PROPOSE, f"{rec['id']} proposed a patch on a regression"
            assert not v.candidates, f"{rec['id']} carries patch candidates on a regression"


def test_selftest_no_changes_no_patch(live_records):
    """Check that the checks are not vacuous: feed the classifier the SAME
    html for baseline and current. Nothing moved, so nothing may be proposed.
    A classifier that always finds an analog fails here."""
    for rec in live_records:
        v = classify_step(rec["baseline_html"], rec["baseline_html"], rec["failure"])
        assert v.action != ACTION_PROPOSE, \
            f"{rec['id']}: proposed a patch when the page did not change"
