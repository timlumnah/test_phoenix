"""v0.1 hand-written suite for the Phoenix Goods demo app.

Locator discipline is the lesson of this file: tests that bind to roles,
labels, and visible text survive the v2 redesign. Tests that bind to class
names die. The intentional deaths are marked ROT: they are the labeled
failures the v0.2 classifier consumes, not sloppiness.
"""
from playwright.sync_api import Page, expect

PRODUCTS = [
    ("CK-2L", "Copper Kettle, 2L", "$48.00"),
    ("CI-10", "Cast Iron Skillet, 10in", "$39.00"),
    ("BW-8", "Beeswax Tin, 8oz", "$12.50"),
    ("HT-1", "Hemp Canvas Tote", "$24.00"),
]


def test_home_lists_all_products(page: Page):
    page.goto("/")
    for _, title, _ in PRODUCTS:
        expect(page.get_by_role("link", name=title)).to_be_visible()


def test_home_shows_prices(page: Page):
    page.goto("/")
    for _, _, price in PRODUCTS:
        expect(page.get_by_text(price, exact=True)).to_be_visible()


def test_product_page_shows_price(page: Page):
    page.goto("/product/CK-2L")
    expect(page.get_by_text("$48.00", exact=True)).to_be_visible()


def test_add_to_cart_updates_badge(page: Page):
    # ROT: role-name locator on the buy button (v2 renames "Add to cart"
    # to "Add item") AND class selector on the badge (v2 renames
    # .cart-count to .cc). Both halves break under v2 by design.
    page.goto("/product/CK-2L")
    page.get_by_role("button", name="Add to cart").click()
    expect(page.locator(".cart-count")).to_have_text("1")


def test_cart_lines_and_total(page: Page):
    page.goto("/product/CK-2L")
    page.get_by_role("button").click()
    page.goto("/product/HT-1")
    page.get_by_role("button").click()
    page.goto("/cart")
    # ROT: class selector for cart rows (v2 renames .cart-line to .c-row).
    expect(page.locator(".cart-line")).to_have_count(2)
    # Stable: visible text.
    expect(page.get_by_text("$72.00", exact=True)).to_be_visible()


def test_checkout_happy_path(page: Page):
    page.goto("/product/BW-8")
    page.get_by_role("button").click()
    page.goto("/checkout")
    page.get_by_label("Name").fill("Ada Fry")
    page.get_by_label("Email").fill("ada@example.com")
    page.get_by_role("button", name="Place order").click()
    expect(page.get_by_role("heading", name="Order confirmed")).to_be_visible()
    expect(page.get_by_text("PHX-")).to_be_visible()


def test_checkout_requires_email(page: Page):
    page.goto("/product/CI-10")
    page.get_by_role("button").click()
    page.goto("/checkout")
    page.get_by_label("Name").fill("Ada Fry")
    page.get_by_role("button", name="Place order").click()
    expect(page.get_by_text("Email is required")).to_be_visible()


def test_empty_cart_cannot_checkout(page: Page):
    page.goto("/checkout")
    page.get_by_label("Name").fill("Ada Fry")
    page.get_by_label("Email").fill("ada@example.com")
    page.get_by_role("button", name="Place order").click()
    expect(page.get_by_text("Cart is empty")).to_be_visible()
