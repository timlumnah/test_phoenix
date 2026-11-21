"""Phoenix Goods demo storefront. Zero JS, zero DB, one file.

Purpose: the selector-rot fixture for test_phoenix. Two markup releases
(RELEASE=v1 or v2) differ ONLY in DOM structure: class names, element ids,
and one button label. Routes, copy, prices, and accessible label text stay
identical. A test suite written against v1 should see exactly the failures
a real site redesign causes: class-based locators break, label/role-based
locators survive.
"""
import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

RELEASE = os.environ.get("RELEASE", "v1")
if RELEASE not in ("v1", "v2", "v2reg"):
    raise SystemExit(f"RELEASE must be v1, v2, or v2reg, got {RELEASE!r}")

PRODUCTS = [
    {"sku": "CK-2L", "title": "Copper Kettle, 2L", "price": 48.00},
    {"sku": "CI-10", "title": "Cast Iron Skillet, 10in", "price": 39.00},
    {"sku": "BW-8", "title": "Beeswax Tin, 8oz", "price": 12.50},
    {"sku": "HT-1", "title": "Hemp Canvas Tote", "price": 24.00},
]
if RELEASE == "v2reg":
    # PLANTED REGRESSION, do not "fix": same redesign as v2 but the kettle's
    # price silently changed 48.00 -> 60.50. The classifier must call the
    # resulting assertion failure a regression and refuse to patch the test.
    PRODUCTS = [dict(p, price=60.50) if p["sku"] == "CK-2L" else p for p in PRODUCTS]
BY_SKU = {p["sku"]: p for p in PRODUCTS}

# Releases differ ONLY in DOM structure (class names, one button's text, the
# badge's position in the DOM). Field ids change too, but labels keep pointing
# at them, so get_by_label keeps working while raw id/class selectors break.
# v2reg reuses the v2 structure and plants a data bug in PRODUCTS above.
DESIGN = {
    "v1": {
        "tile": "product-card",
        "price": "product-price",
        "count": "cart-count",
        "line": "cart-line",
        "total": "cart-total",
        "error": "form-error",
        "buy_text": "Add to cart",
        "name_id": "field-name",
        "email_id": "field-email",
        "badge_after_nav": False,
    },
    "v2": {
        "tile": "p-tile",
        "price": "p-cost",
        "count": "cc",
        "line": "c-row",
        "total": "c-sum",
        "error": "err-box",
        "buy_text": "Add item",
        "name_id": "fName",
        "email_id": "fEmail",
        "badge_after_nav": True,
    },
}
# v2reg ships the v2 design; its planted bug is data, not markup
CLS = DESIGN["v1" if RELEASE == "v1" else "v2"]

C = CLS
app = FastAPI()
_order_seq = {"n": 0}


def money(v: float) -> str:
    return f"${v:.2f}"


def cart_skus(request: Request) -> list[str]:
    raw = request.cookies.get("cart", "")
    return [s for s in raw.split(",") if s in BY_SKU]


def cart_total(skus: list[str]) -> float:
    return sum(BY_SKU[s]["price"] for s in skus)


def render(title: str, body: str, count: int) -> HTMLResponse:
    badge = f"<span class='{C['count']}'>{count}</span>"
    nav = f"<header><a href='/' class='brand'>Phoenix Goods</a> <a href='/cart'>Cart</a> {badge}</header>"
    if C["badge_after_nav"]:
        nav = nav.replace(badge, "") + badge
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title} | Phoenix Goods</title></head><body>{nav}<main>{body}</main>"
        f"<footer>demo app, release {RELEASE}</footer></body></html>"
    )
    return HTMLResponse(html)


@app.get("/health")
def health():
    return JSONResponse({"ok": True, "release": RELEASE})


@app.get("/")
def home(request: Request):
    cards = ""
    for p in PRODUCTS:
        cards += (
            f"<li class='{C['tile']}'><a href='/product/{p['sku']}'>{p['title']}</a>"
            f"<span class='{C['price']}'>{money(p['price'])}</span></li>"
        )
    body = f"<h1>Goods for the long winter</h1><ul class='product-list'>{cards}</ul>"
    return render("Home", body, len(cart_skus(request)))


@app.get("/product/{sku}")
def product(sku: str, request: Request):
    if sku not in BY_SKU:
        return render("Not found", "<h1>No such product</h1>", len(cart_skus(request)))
    p = BY_SKU[sku]
    body = (
        f"<h1>{p['title']}</h1><p class='{C['price']}'>{money(p['price'])}</p>"
        f"<form method='post' action='/cart/add'><input type='hidden' name='sku' value='{sku}'>"
        f"<button type='submit'>{C['buy_text']}</button></form>"
    )
    return render(p["title"], body, len(cart_skus(request)))


@app.post("/cart/add")
async def add(request: Request):
    form = await request.form()
    sku = form.get("sku", "")
    resp = RedirectResponse("/cart", status_code=303)
    if sku in BY_SKU:
        skus = cart_skus(request) + [sku]
        resp.set_cookie("cart", ",".join(skus))
    return resp


@app.get("/cart")
def cart(request: Request):
    skus = cart_skus(request)
    if not skus:
        body = "<h1>Cart</h1><p>Cart is empty.</p>"
        return render("Cart", body, 0)
    rows = ""
    for s in skus:
        p = BY_SKU[s]
        rows += f"<tr class='{C['line']}'><td>{p['title']}</td><td>{money(p['price'])}</td></tr>"
    body = (
        "<h1>Cart</h1><table><tbody>" + rows + "</tbody></table>"
        f"<p>Total: <span class='{C['total']}'>{money(cart_total(skus))}</span></p>"
        "<a href='/checkout'>Checkout</a>"
    )
    return render("Cart", body, len(skus))


@app.get("/checkout")
def checkout(request: Request):
    skus = cart_skus(request)
    body = (
        "<h1>Checkout</h1>"
        "<form method='post' action='/checkout'>"
        f"<label for='{C['name_id']}'>Name</label>"
        f"<input id='{C['name_id']}' name='name'>"
        f"<label for='{C['email_id']}'>Email</label>"
        f"<input id='{C['email_id']}' name='email'>"
        "<button type='submit'>Place order</button>"
        "</form>"
    )
    return render("Checkout", body, len(skus))


@app.post("/checkout")
async def do_checkout(request: Request):
    skus = cart_skus(request)
    form = await request.form()
    name = str(form.get("name", "")).strip()
    email = str(form.get("email", "")).strip()
    errors = ""
    if not name:
        errors += "<p class='err'>Name is required</p>"
    if not email or "@" not in email:
        errors += "<p class='err'>Email is required</p>"
    if not skus:
        errors += "<p class='err'>Cart is empty</p>"
    if errors:
        body = f"<h1>Checkout</h1><div class='{C['error']}'>{errors}</div>"
        return render("Checkout", body, len(skus))
    _order_seq["n"] += 1
    ref = f"PHX-{_order_seq['n']:04d}-{hash(email) % 9973:04d}"
    resp = RedirectResponse(f"/thanks?ref={ref}", status_code=303)
    resp.delete_cookie("cart")
    return resp


@app.get("/thanks")
def thanks(request: Request, ref: str = ""):
    if not ref.startswith("PHX-"):
        ref = "PHX-????"
    body = f"<h1>Order confirmed</h1><p>Your reference: {ref}</p>"
    return render("Thanks", body, 0)
