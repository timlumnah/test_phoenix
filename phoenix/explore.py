"""Explorer: cold-start from a bare URL, produce an observed ground truth.

The catalog is the ONLY source of facts the drafter may assert against. It
records, per page: heading, visible button/link texts, form labels, and the
results of two safe probes per form: empty submit and valid submit with
TEST-ONLY input values (inputs are requests, never claims; the resulting
error/success copy is what becomes assertable).

Note: same-origin GET crawl only; no JS beyond what the app itself
does (the demo app ships none). Breadth caps keep the crawl finite.

v0.5 (external targets): hrefs resolve like a browser (relative, path,
same-origin absolute); a page whose goto fails (auth walls, timeouts) is
skipped and recorded, never fatal; form probes survive exotic fields
(<select> gets select_option, an unfillable field is skipped); the add-flow
policy is a parameter so third-party apps define their own "action page".
"""
from __future__ import annotations

import json
import os
from urllib.parse import urljoin, urlparse

from phoenix.capture import Server
from playwright.sync_api import sync_playwright

MAX_PAGES = 12
MAX_FLOWS = 6
GOTO_TIMEOUT_MS = 15000
TEST_INPUTS = {"email": "test@example.com", "text": "Test Person", "": "Test Value"}

_SKIP_PREFIXES = ("mailto:", "javascript:", "tel:", "data:", "#")


def resolve_href(href: str, page_url: str, origin: str) -> str | None:
    """Same-origin path for an href, resolved the way a browser resolves it:
    relative hrefs against the current page URL, absolute paths from root,
    same-origin URLs truncated to path. Cross-origin and pseudo-hrefs -> None.
    Query and fragment are dropped: the catalog keys pages by path."""
    if not href or href.startswith(_SKIP_PREFIXES):
        return None
    href = href.split("#")[0].split("?")[0]
    if not href:
        return None
    try:
        url = urljoin(page_url, href)
    except ValueError:
        return None
    p = urlparse(url)
    if f"{p.scheme}://{p.netloc}" != origin:
        return None
    return p.path or "/"


def _safe_goto(page, url: str) -> bool:
    """Third-party pages fail: 401 prompts, slow redirects, dead servers.
    A failed navigation skips the page; the crawl continues."""
    try:
        page.goto(url, timeout=GOTO_TIMEOUT_MS, wait_until="domcontentloaded")
        return True
    except Exception:
        return False


def page_observables(page) -> dict:
    def texts(loc):
        try:
            return [t.strip() for t in loc.all_inner_texts() if t.strip()]
        except Exception:
            return []
    h1 = ""
    try:
        h1 = page.locator("h1").first.inner_text().strip()
    except Exception:
        pass
    buttons = texts(page.get_by_role("button"))
    links = texts(page.get_by_role("link"))
    labels = texts(page.locator("label"))
    select_labels = _select_labels(page)
    pairs = []
    for a in page.locator("a").all():
        try:
            t = a.inner_text().strip()
            href = a.get_attribute("href") or ""
        except Exception:
            continue
        if t and href.startswith("/"):
            pairs.append([t, href.split("?")[0]])   # already a path, strip query
    # Leaf text, captured in the browser: for every element under <main>
    # whose own children are all text nodes, take its trimmed text. This is
    # state-accurate and does not merge inline siblings the way a container
    # innerText split does (title and price are separate leaves, never
    # "Copper Kettle, 2L$48.00").
    body = _leaf_texts(page)
    return {"h1": h1, "buttons": buttons, "links": links, "labels": labels,
            "select_labels": select_labels,
            "link_pairs": pairs, "main_texts": body}


_SELECT_LABELS_JS = """() => {
  const out = [];
  for (const lab of document.querySelectorAll('label')) {
    let ctl = lab.htmlFor ? document.getElementById(lab.htmlFor) : null;
    if (!ctl) ctl = lab.querySelector('select');
    if (ctl && ctl.tagName === 'SELECT') {
      const t = lab.textContent.trim();
      if (t) out.push(t);
    }
  }
  return [...new Set(out)];
}"""


def _select_labels(page) -> list[str]:
    """Labels whose control is a <select>: fill() cannot drive those, so the
    drafter and the gate must know they are selects, not text fields."""
    try:
        return [t for t in page.evaluate(_SELECT_LABELS_JS) if isinstance(t, str)]
    except Exception:
        return []


_LEAF_JS = """() => {
  const out = [];
  const main = document.querySelector('main') || document.body;
  const walk = (el) => {
    // this element's own direct text (excludes text inside children)
    let own = '';
    for (const n of el.childNodes) {
      if (n.nodeType === 3) own += n.textContent;
    }
    own = own.trim();
    if (own) out.push(own);
    if (el.children.length === 0) return;
    for (const c of el.children) walk(c);
  };
  walk(main);
  return [...new Set(out)];
}"""


def _leaf_texts(page) -> list[str]:
    try:
        return [t for t in page.evaluate(_LEAF_JS) if isinstance(t, str) and t]
    except Exception:
        return []


def normalize_path(url: str, origin: str) -> str:
    return url.replace(origin, "").split("?")[0] or "/"


def explore(base: str, pw=None, flow_policy=None) -> dict:
    """base: http://host[:port] or that plus a start path (third-party apps
    often put the entry point at /login, not /). pw: an existing
    sync_playwright instance (pytest-playwright's session fixture) to share;
    when None, one is opened and closed here. Sharing prevents the
    'Sync API inside the asyncio loop' collision when tests/ and evals run
    in the same process in any order.
    flow_policy: callable(path, obs) -> bool deciding which pages get an
    add-flow click probe (default: the demo app's /product/ pages)."""
    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    start = parsed.path or "/"
    policy = flow_policy or _default_flow_policy
    catalog = {"origin": origin, "start": start, "pages": {}, "probes": {},
               "flow": {}, "skipped": [], "probe_errors": []}
    own = pw is None
    if own:
        pw = sync_playwright().start()
    browser = pw.chromium.launch()
    try:
        page = browser.new_page()
        queue = [start]
        seen = set()
        while queue and len(seen) < MAX_PAGES:
            path = queue.pop(0)
            if path in seen:
                continue
            seen.add(path)
            if not _safe_goto(page, origin + path):
                catalog["skipped"].append(path)
                continue
            obs = page_observables(page)
            catalog["pages"][path] = obs
            for link in page.locator("a").all():
                try:
                    href = link.get_attribute("href") or ""
                except Exception:
                    continue
                resolved = resolve_href(href, page.url, origin)
                if resolved and resolved not in seen:
                    queue.append(resolved)
            _probe_forms(browser, origin, path, catalog)
        # record where add-to-cart buttons lead (redirect target after click)
        _probe_add_flows(browser, origin, catalog, seen, policy)
        # second pass: add flows and form probes reveal pages the static
        # link graph hides (empty /cart links to nothing; a cart with an
        # item links to /checkout). Visit and probe those too, bounded.
        extra = {fl["lands_on"] for fl in catalog["flow"].values()}
        for snap in list(catalog["flow"].values()):
            extra.update(snap["landing_page"].get("link_paths", []))
        for p in catalog["probes"].values():
            for res in ("empty_result", "valid_result"):
                extra.update(p[res].get("link_paths", []))
        for path in sorted(extra):
            if path in catalog["pages"] or len(catalog["pages"]) >= MAX_PAGES:
                continue
            if not _safe_goto(page, origin + path):
                catalog["skipped"].append(path)
                continue
            catalog["pages"][path] = page_observables(page)
            _probe_forms(browser, origin, path, catalog)
        # third pass: a labeled form may only succeed AFTER a cart
        # interaction. Probe labeled forms again with an explicit observed
        # add-flow as setup, so every recorded success can be replayed.
        setup_flows = sorted(catalog["flow"])
        for setup in setup_flows[:2]:
            for path in sorted(catalog["pages"]):
                obs = catalog["pages"][path]
                if not obs.get("labels"):
                    continue
                _probe_forms(browser, origin, path, catalog, setup=setup)
        page.close()
    finally:
        browser.close()
        if own and pw is not None:
            pw.stop()          # stop lives on the Playwright object
    return catalog


def _probe_forms(browser, origin: str, path: str, catalog: dict, setup: str | None = None):
    """Each probe starts from a FRESH page: no cart cookies, no leftovers.
    A setup flow (observed add path) is replayed first when given, so the
    recorded success is exactly replayable. A form whose probe raises (a
    field type we cannot fill, a navigation that dies) is recorded in
    probe_errors and skipped; on third-party markup, one bad form must
    never kill the crawl."""
    page = browser.new_page()
    try:
        if setup:
            if not _safe_goto(page, origin + setup):
                return
            try:
                page.get_by_role("button").first.click()   # lands by flow record
            except Exception:
                return
        if not _safe_goto(page, origin + path):
            return
        for i in range(page.locator("form").count()):
            try:
                _probe_one_form(page, origin, path, i, setup, catalog)
            except Exception as e:
                key = f"{path}#{i}" + (f"@setup:{setup}" if setup else "")
                catalog["probe_errors"].append(f"{key}: {type(e).__name__}: {str(e)[:120]}")
    finally:
        page.close()


def _fill_field(inp, lab: str):
    """Fill a labeled field with a test-only value. Selects get their first
    real option (fill() throws on <select>); anything still unfillable is
    left empty: the empty-submit copy for that field is itself an observation."""
    tag = inp.evaluate("e => e.tagName")
    if tag == "SELECT":
        inp.select_option(index=1)
    else:
        kind = "email" if "email" in lab.lower() else "text"
        inp.fill(TEST_INPUTS[kind])


def _probe_one_form(page, origin, path, i, setup, catalog):
    form = page.locator("form").nth(i)
    submits = form.get_by_role("button")
    if submits.count() == 0:
        return
    btn = submits.first.inner_text().strip()
    # probe 1: empty submit, what does the app say about missing input?
    if not _safe_goto(page, origin + path):
        return
    page.locator("form").nth(i).get_by_role("button").first.click()
    empty_result = _snap(page, origin)
    # probe 2: valid submit with test-only values (inputs are requests,
    # never claims; the resulting copy is what becomes assertable)
    if not _safe_goto(page, origin + path):
        return
    for lab in page.locator("form").nth(i).locator("label").all_inner_texts():
        inp = page.get_by_label(lab.strip())
        if inp.count():
            try:
                _fill_field(inp.first, lab.strip())
            except Exception:
                continue
    page.locator("form").nth(i).get_by_role("button").first.click()
    valid_result = _snap(page, origin)
    key = f"{path}#{i}" + (f"@setup:{setup}" if setup else "")
    catalog["probes"][key] = {
        "page": path, "submit_button": btn,
        "setup": setup,
        "empty_result": empty_result,
        "valid_result": valid_result,
    }


def _snap(page, origin: str) -> dict:
    links = []
    for a in page.locator("a").all():
        try:
            href = a.get_attribute("href") or ""
        except Exception:
            continue
        if href.startswith("/"):
            links.append(normalize_path(origin + href, origin))
    return {"path": normalize_path(page.url, origin),
            "obs": page_observables(page), "link_paths": links}


def _merge_obs(target: dict, src: dict):
    """Fold an observed render of a page into the page's record: state that
    was only ever seen in one condition (a cart WITH an item) is still
    observed on that page. Verification, not the checker, is what rejects
    asserting a state you did not set up."""
    if not src:
        return
    if src.get("h1") and not target.get("h1"):
        target["h1"] = src["h1"]
    for k in ("buttons", "links", "labels", "select_labels", "link_pairs",
              "main_texts"):
        cur = target.setdefault(k, [])
        for item in src.get(k, []):
            if item not in cur:
                cur.append(item)


def _default_flow_policy(path: str, obs: dict) -> bool:
    """The demo app's add-to-cart pages. External targets pass their own."""
    return "/product/" in path


def _action_page_policy(path: str, obs: dict) -> bool:
    """v0.5 external breadth: an 'action page' shows buttons but carries no
    form (forms are probed already) and is not the root page. Clicking its
    first button and recording the landing is the same add-flow probe the
    demo app gets on /product/ pages."""
    return path != "/" and bool(obs.get("buttons")) and not obs.get("labels")


def _probe_add_flows(browser, origin: str, catalog: dict, pages: set,
                     policy=_default_flow_policy):
    """For pages the flow policy picks, click the first button and record
    where we land and what the landing page shows, per starting page."""
    probed = 0
    for path in sorted(pages):
        if probed >= MAX_FLOWS:
            break
        obs = catalog["pages"].get(path)
        if not obs or not obs["buttons"] or not policy(path, obs):
            continue
        page = browser.new_page()
        try:
            if not _safe_goto(page, origin + path):
                continue
            try:
                page.get_by_role("button").first.click()
            except Exception:
                continue
            try:   # bounded settle: third-party pages may keep sockets open
                page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            landed = _snap(page, origin)
            catalog["flow"][path] = {
                "clicked_button": obs["buttons"][0],
                "lands_on": landed["path"],
                "landing_page": landed["obs"],
            }
            probed += 1
            # the populated cart is still THE CART page: enrich its record
            _merge_obs(catalog["pages"].setdefault(landed["path"], {}),
                       landed["obs"])
        finally:
            page.close()


def explore_release(release: str, pw=None) -> dict:
    srv = Server(release)
    try:
        return explore(srv.url, pw=pw)
    finally:
        srv.stop()


if __name__ == "__main__":
    cat = explore_release("v1")
    print(json.dumps(cat, indent=1)[:4000])
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "drafts", "catalog_v1.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(cat, f, indent=1)
    print(f"\ncatalog written to {out}: {len(cat['pages'])} pages, "
          f"{len(cat['probes'])} form probes, {len(cat['flow'])} add flows")
