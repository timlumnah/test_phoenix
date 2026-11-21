"""Tiny DOM snapshot model built on stdlib html.parser. No bs4, no browser.

Purpose: let the classifier compare a baseline page against the current page
as flat element records with structural signatures (tag, attributes, normalized
text, parent, position among same-tag siblings). Class renames, id renames,
and element moves are exactly the things these signatures see through.

Note: pages in this project are small (single-file demo app), so the
records list is rebuilt per parse; no incremental parsing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"}


@dataclass
class ElementRecord:
    tag: str
    attrs: dict = field(default_factory=dict)
    text: str = ""
    parent_tag: str = ""
    parent_path: tuple = ()      # ancestor tags root->parent
    sibling_index: int = 0       # index among same-tag children of parent
    depth: int = 0

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def selector_signature(self) -> str:
        ids = f"#{self.attrs['id']}" if "id" in self.attrs else ""
        cls = "." + self.classes[0] if self.classes else ""
        return f"{self.tag}{ids}{cls}"

    def structural_key(self) -> tuple:
        """Identity ignoring class/id: what a human would still call the same widget."""
        return (self.tag, self.text, self.parent_tag, self.sibling_index)


def _norm(s: str) -> str:
    return " ".join(s.split())


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict):
        self.tag = tag
        self.attrs = attrs
        self.children: list["_Node"] = []

    def text(self) -> str:
        out = []
        for c in self.children:
            out.append(c.attrs["_data"] if c.tag == "#text" else c.text())
        return " ".join(t for t in out if t)


class _TreeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        ad: dict[str, str] = {}
        for k, v in attrs:
            if k not in ad:      # first wins, matches browser attribute parsing
                ad[k] = v or ""
        node = _Node(tag, ad)
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_data(self, data):
        if data.strip():
            self.stack[-1].children.append(_Node("#text", {"_data": data}))

    def handle_endtag(self, tag):
        # pop to the matching open tag; tolerate sloppy or unbalanced html
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return


def parse_html(html_str: str) -> list[ElementRecord]:
    """Flatten a document into ElementRecords, document order, text bubbling."""
    parser = _TreeParser()
    parser.feed(html_str)
    records: list[ElementRecord] = []

    def walk(node: _Node, ancestors: tuple, depth: int):
        counters: dict[str, int] = {}
        for child in node.children:
            if child.tag == "#text":
                continue
            idx = counters.get(child.tag, 0)
            counters[child.tag] = idx + 1
            records.append(ElementRecord(
                tag=child.tag,
                attrs=dict(child.attrs),
                text=_norm(child.text()),
                parent_tag=node.tag if node.tag != "#root" else "",
                parent_path=ancestors,
                sibling_index=idx,
                depth=depth,
            ))
            walk(child, ancestors + (child.tag,), depth + 1)

    walk(parser.root, (), 0)
    return records


# --- minimal selector matching (what the classifier needs, nothing more) ---

def parse_selector(sel: str) -> dict:
    """Support 'tag', '.class', '#id', 'tag.class', 'button[name]' style is
    out of scope; only the first class/id/tag compound is parsed."""
    out = {"tag": "", "class": "", "id": ""}
    for part in _split_selector(sel):
        if part.startswith("."):
            out["class"] = part[1:]
        elif part.startswith("#"):
            out["id"] = part[1:]
        else:
            out["tag"] = part
    return out


def _split_selector(sel: str) -> list[str]:
    parts, cur = [], ""
    for ch in sel:
        if ch in ".#":
            if cur:
                parts.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        parts.append(cur)
    return parts


def matches(rec: ElementRecord, sel: dict) -> bool:
    if sel["tag"] and rec.tag != sel["tag"]:
        return False
    if sel["class"] and sel["class"] not in rec.classes:
        return False
    if sel["id"] and rec.attrs.get("id") != sel["id"]:
        return False
    return True


def find(records: list[ElementRecord], selector: str) -> list[ElementRecord]:
    sel = parse_selector(selector)
    return [r for r in records if matches(r, sel)]
