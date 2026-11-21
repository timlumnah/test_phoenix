"""v0.4 eval: the draft gate must accept only what the catalog observed,
and must reject invented facts, misclicked roles, unknown routes, bad names,
and model junk. The renderer must produce valid, faithful code.

Fixture catalog: a tiny stand-in mirroring the real app's observed structure
(page records, one plain form probe, one add flow), so gate behavior is
checkable without a browser.
"""
import ast
import json

import pytest

from phoenix.draft import (DraftError, check_draft, llm_draft, render,
                           rule_draft)
from phoenix.model import Scripted


def _obs(h1="", buttons=(), links=(), labels=(), texts=(), pairs=()):
    return {"h1": h1, "buttons": list(buttons), "links": list(links),
            "labels": list(labels), "link_pairs": [list(p) for p in pairs],
            "main_texts": list(texts)}


CATALOG = {
    "origin": "http://x",
    "pages": {
        "/": _obs("Goods", links=["Beeswax Tin", "Cart"],
                  texts=["Beeswax Tin", "$12.50"],
                  pairs=[["Beeswax Tin", "/product/bw"], ["Cart", "/cart"]]),
        "/cart": _obs("Cart", links=["Phoenix Goods", "Cart"],
                      texts=["Cart", "Cart is empty."]),
        "/product/bw": _obs("Beeswax Tin", buttons=["Add to cart"],
                            texts=["Beeswax Tin", "$12.50"]),
        "/checkout": _obs("Checkout", buttons=["Place order"],
                          labels=["Name", "Email"],
                          texts=["Checkout", "Name", "Email", "Place order"]),
    },
    "probes": {
        "/checkout#0": {
            "page": "/checkout", "submit_button": "Place order", "setup": None,
            "empty_result": {"path": "/checkout", "link_paths": [],
                             "obs": _obs("Checkout", buttons=["Place order"],
                                         labels=["Name", "Email"],
                                         texts=["Checkout", "Name is required",
                                                "Email is required"])},
            "valid_result": {"path": "/checkout", "link_paths": [],
                             "obs": _obs("Checkout", buttons=["Place order"],
                                         labels=["Name", "Email"],
                                         texts=["Checkout", "Cart is empty"])},
        },
    },
    "flow": {
        "/product/bw": {"clicked_button": "Add to cart", "lands_on": "/checkout",
                        "landing_page": _obs("Checkout", buttons=["Place order"],
                                             labels=["Name", "Email"],
                                             texts=["Checkout", "Beeswax Tin",
                                                    "$12.50"])},
    },
}


def _test(name, *steps):
    return {"tests": [{"name": name, "steps": list(steps)}]}


def test_clean_draft_passes_gate():
    d = _test("test_ok", {"goto": "/checkout"},
              {"click_button_named": "Place order"},
              {"expect_text": "Name is required"})
    assert check_draft(CATALOG, d) == []


def test_button_click_transitions_only_along_recorded_flow():
    # explorer SAW Add to cart land on /checkout for this product: asserting
    # the landing page's checkout button afterwards is legitimate
    d = _test("test_flow", {"goto": "/product/bw"},
              {"click_button_named": "Add to cart"},
              {"click_button_named": "Place order"},
              {"expect_text": "Beeswax Tin"})
    assert check_draft(CATALOG, d) == []


def test_link_click_transitions_along_recorded_href():
    d = _test("test_linknav", {"goto": "/"},
              {"click_link_named": "Beeswax Tin"},
              {"expect_heading": "Beeswax Tin"},
              {"click_button_named": "Add to cart"})
    assert check_draft(CATALOG, d) == []


def test_stale_context_assertion_rejected():
    # /cart never shows the checkout error copy: the gate must not carry
    # strings across pages that were not visited
    d = _test("test_stale", {"goto": "/cart"},
              {"expect_text": "Name is required"})
    assert any("invented fact" in x for x in check_draft(CATALOG, d))


def test_assertion_before_goto_rejected():
    d = _test("test_orphan", {"expect_text": "Cart is empty."})
    assert any("before any goto" in x for x in check_draft(CATALOG, d))


def test_enriched_page_state_allows_flow_navigation():
    """Regression for the live-model false rejection (2026-11-20): the
    populated cart render shows a Checkout link the empty cart never does.
    When the explorer folds a landing render into the page record (the
    behavior _merge_obs now guarantees), goto-then-click must be legal.
    State discipline is then enforced by the 3x verification gate, not by
    the checker: a test that fakes the state fails when it runs."""
    cat = json.loads(json.dumps(CATALOG))        # deep copy
    cat["pages"]["/cart"]["links"].append("Checkout")
    cat["pages"]["/cart"]["link_pairs"].append(["Checkout", "/checkout"])
    d = _test("test_state_nav", {"goto": "/cart"},
              {"click_link_named": "Checkout"},
              {"expect_heading": "Checkout"},
              {"fill_label": ["Name", "Test Person"]})
    assert check_draft(cat, d) == []


def test_invented_fact_rejected():
    d = _test("test_lie", {"goto": "/checkout"},
              {"expect_text": "Free shipping on orders over $50"})
    v = check_draft(CATALOG, d)
    assert v and "invented fact" in v[0]


def test_invented_summed_total_rejected():
    # the exact failure mode the live Ollama draft showed: the model does
    # arithmetic the app never displayed
    d = _test("test_math", {"goto": "/product/bw"},
              {"expect_text": "$25.00"})
    assert any("invented" in x or "never observed" in x
               for x in check_draft(CATALOG, d))


def test_unknown_route_rejected():
    d = _test("test_404", {"goto": "/admin"})
    assert any("unobserved path" in x for x in check_draft(CATALOG, d))


def test_button_link_confusion_rejected():
    d = _test("test_role", {"goto": "/checkout"},
              {"click_link_named": "Place order"})       # it is a button
    assert any("not observed" in x for x in check_draft(CATALOG, d))


def test_unknown_label_rejected():
    d = _test("test_lab", {"goto": "/checkout"},
              {"fill_label": ["Credit card", "4242"]})
    assert any("label" in x for x in check_draft(CATALOG, d))


def test_bad_and_duplicate_names_rejected():
    d = {"tests": [{"name": "not_a_test", "steps": []},
                   {"name": "test_dup", "steps": []},
                   {"name": "test_dup", "steps": []}]}
    v = check_draft(CATALOG, d)
    assert any("bad test name" in x for x in v)
    assert any("duplicate" in x for x in v)


def test_unknown_step_kind_rejected():
    d = _test("test_evil", {"run_js": "alert(1)"})
    assert any("unknown step kind" in x for x in check_draft(CATALOG, d))


def test_renderer_output_is_valid_and_faithful():
    d = _test("test_render", {"goto": "/checkout"},
              {"fill_label": ["Email", "a@b.c"]},
              {"click_button_named": "Place order"},
              {"click_link_named": "Cart"},
              {"expect_heading": "Checkout"},
              {"expect_text": "Name is required"},
              {"expect_button_named": "Place order"},
              {"expect_link_named": "Cart"},
              {"expect_label": "Email"})
    src = render(d)
    ast.parse(src)                                  # compiles
    assert 'page.goto("/checkout")' in src
    assert 'get_by_role("button", name="Place order", exact=True).click()' in src
    assert 'get_by_role("link", name="Cart", exact=True).click()' in src
    assert 'get_by_label("Email", exact=True).fill("a@b.c")' in src
    assert src.count("def test_") == 1


def test_llm_draft_rejects_nonjson():
    model = Scripted(["<marquee>not json</marquee>"])
    with pytest.raises(DraftError):
        llm_draft(CATALOG, model)


def test_llm_draft_judged_like_any_other():
    good = ('{"tests": [{"name": "test_home", "steps": '
            '[{"goto": "/"}, {"expect_heading": "Goods"}]}]}')
    draft = llm_draft(CATALOG, Scripted([good]))
    assert check_draft(CATALOG, draft) == []
    lie = ('{"tests": [{"name": "test_lie", "steps": [{"goto": "/"}, '
           '{"expect_text": "Lifetime warranty"}]}]}')
    assert any("invented" in x for x in
               check_draft(CATALOG, llm_draft(CATALOG, Scripted([lie]))))


def test_rule_draft_is_always_clean_on_catalog():
    """The rule drafter defines the ceiling; it must pass its own gate."""
    d = rule_draft(CATALOG)
    assert d["tests"]
    assert check_draft(CATALOG, d) == []


def test_rule_draft_replays_setup_for_success_forms():
    # fixture has no setup-replayed probe, so the /checkout success test is
    # only emitted when a real success was observed: here, only the reject test
    d = rule_draft(CATALOG)
    names = [t["name"] for t in d["tests"]]
    assert "test__checkout_rejects_empty_form" in names
    assert not any("accepts_valid" in n for n in names)
