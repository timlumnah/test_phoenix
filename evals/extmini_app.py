"""A stand-in 'third-party app' for the v0.5 external-target evals.

Nothing here is the demo app: pages are reached via RELATIVE hrefs, the one
form contains a <select> (fill() throws on selects), one route hides behind
HTTP basic auth (goto must fail gracefully), one page is an 'action page'
(button, no form) exercising the flow policy, and a successful submit
redirects to a page with no links back into the static graph. This file is
test scaffolding; it is never a demo or a target of its own pipeline.
"""
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI()


def _page(h1: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<html><head><title>extmini</title></head><body><main>"
        f"<h1>{h1}</h1>{body}</main></body></html>")


@app.get("/")
def home():
    return _page("Mini Home",
                 "<ul>"
                 '<li><a href="one">Relative one</a></li>'
                 '<li><a href="/abs">Absolute path</a></li>'
                 '<li><a href="sub/deep">Deep relative</a></li>'
                 '<li><a href="act">Do a thing</a></li>'
                 '<li><a href="/secret">Locked</a></li>'
                 '<li><a href="http://elsewhere.invalid/x">Outside</a></li>'
                 "</ul>")


@app.get("/one")
def one():
    return _page("Form One",
                 '<form action="/one" method="post">'
                 '<label for="color">Favourite color</label>'
                 '<select id="color" name="color"><option value="">Choose</option>'
                 '<option value="red">Red</option></select>'
                 '<label for="note">Note</label>'
                 '<input id="note" name="note" type="text">'
                 '<button type="submit">Save</button></form>'
                 '<p>Fill the note to save</p>')


@app.post("/one")
async def one_submit(req: Request):
    body = await req.form()
    note = (body.get("note") or "").strip()
    if not note:
        return _page("Form One", '<p>Fill the note to save</p>'
                     "<p>Please add a note first</p>")
    return RedirectResponse("/done", status_code=303)


@app.get("/done")
def done():
    return _page("All Done", "<p>Nothing more to see</p>")


@app.get("/abs")
def absx():
    return _page("Abs Page", '<p>Stable copy here</p><a href="one">To one</a>')


@app.get("/sub/deep")
def deep():
    return _page("Deep Page", '<a href="../one">Up and over</a>')


@app.get("/act")
def act():
    return _page("Actors",
                 '<button type="button" '
                 'onclick="document.getElementById(\'out\').textContent='
                 '\'Thing added\'; this.nextElementSibling.style.display=\'\'">'
                 "Add Thing</button>"
                 '<button type="button" style="display:none">Remove Thing</button>'
                 '<p id="out"></p>')


@app.get("/secret")
def secret():
    return Response(status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="locked"'})
