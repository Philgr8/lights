#!/usr/bin/env python3
"""Telegram Bot API 10.4 widget demo: owner-queue panel.

Offline by default: builds the widget payload (sendWidget params), validates it
against the documented limits, and answers widget_query updates with a pure
function. Posting needs --send CHAT_ID and TG_BOT_TOKEN in env (never run by
selftest). Docs: https://core.telegram.org/bots/api (10.4), /bots/widgets.
"""
import argparse
import json
import os
import sys
import urllib.request
import zlib

HTML_MAX = 32768
HTML_ZLIB_MAX = 4096
DATA_MAX = 1024
QUERY_DATA_MAX = 64
ANSWER_MAX = 1024
FORBIDDEN = ("<iframe", "<video", "<audio", "<input", "<select", "<textarea",
             "alert(", "confirm(", "prompt(", "fetch(", "XMLHttpRequest",
             "WebSocket", "overflow:auto", "overflow:scroll", "sticky",
             "http://", "https://", "<script src", "<link", "100vh")

CSS = """
*{box-sizing:border-box;margin:0}
body{font:var(--tg-text-font-size,14px)/1.3 system-ui;color:var(--tg-theme-text-color);
background:var(--tg-theme-bg-color);padding:10px 12px calc(10px + var(--tg-content-safe-area-inset-bottom,0px))}
h1{font-size:15px;display:flex;justify-content:space-between;align-items:center}
.n{background:var(--tg-theme-button-color);color:var(--tg-theme-button-text-color);
border-radius:10px;padding:1px 8px;font-size:12px}
.r{display:flex;gap:8px;align-items:center;padding:6px 8px;margin-top:6px;border-radius:10px;
background:var(--tg-theme-secondary-bg-color)}
.d{width:8px;height:8px;border-radius:50%;flex:none}
.hi{background:var(--tg-theme-destructive-text-color)}
.md{background:var(--tg-theme-accent-text-color)}
.lo{background:var(--tg-theme-hint-color)}
.t{flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.a{color:var(--tg-theme-hint-color);font-size:12px}
button{margin-top:8px;width:100%}
.e{color:var(--tg-theme-hint-color);font-size:12px;margin-top:6px}
""".strip()

JS = """
var W=window.TelegramWidget;
function S(){var s=null;try{s=JSON.parse(W.chatData)}catch(e){}return s}
function esc(x){return String(x).replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c]})}
function draw(){var s=S(),q=document.getElementById('q');
if(!s){q.innerHTML='<div class="e">no data</div>';return}
document.getElementById('c').textContent=s.n;
q.innerHTML=s.i.map(function(i){return'<div class="r"><span class="d '+i[0]+'"></span><span class="t">'
+esc(i[1])+'</span><span class="a">'+esc(i[2])+'</span></div>'}).join('')}
document.getElementById('b').onclick=function(){
W.HapticFeedback.impactOccurred('light');
W.request('refresh').then(function(r){document.getElementById('m').textContent='ok '+r.result})
.catch(function(e){document.getElementById('m').textContent=e.code})};
W.onEvent('chatDataChanged',draw);draw();W.ready();
""".strip()


def build_html() -> str:
    return ("<style>" + CSS + "</style><h1>Owner queue<span class=n id=c>0</span></h1>"
            "<div id=q></div><button id=b>Refresh</button><div class=e id=m></div>"
            "<script>" + JS + "</script>")


def build_chat_data(items) -> str:
    """items: [(prio 'hi|md|lo', title, age)]; compact JSON, <=1024 bytes."""
    out = []
    for p, t, a in items:
        out.append([p, t[:40], a])
        while len(json.dumps({"n": len(items), "i": out}, separators=(",", ":"),
                             ensure_ascii=False).encode()) > DATA_MAX:
            out.pop()
    return json.dumps({"n": len(items), "i": out}, separators=(",", ":"), ensure_ascii=False)


DEMO_ITEMS = [("hi", "Arkham API call needs yes", "2d"),
              ("hi", "ACLED credentials reset", "1d"),
              ("md", "Buyer contact names", "5h"),
              ("md", "Paid wallet history pull", "3h"),
              ("lo", "Weekly digest review", "1h")]


def build_send_params(chat_id, items=DEMO_ITEMS) -> dict:
    return {"chat_id": chat_id, "html": build_html(), "width": 360, "height": 300,
            "chat_data": build_chat_data(items), "allow_queries": True,
            "caption": "Owner queue", "disable_notification": True}


def validate(p: dict) -> list:
    errs = []
    h = p["html"].encode()
    if not 1 <= len(h) <= HTML_MAX:
        errs.append("html size %d" % len(h))
    z = len(zlib.compress(h, 9))
    if z > HTML_ZLIB_MAX:
        errs.append("zlib size %d > %d" % (z, HTML_ZLIB_MAX))
    for k in ("chat_data", "public_data"):
        if k in p and len(p[k].encode()) > DATA_MAX:
            errs.append(k + " too big")
    if "chat_data" in p:
        json.loads(p["chat_data"])
    if not 200 <= p.get("width", 200) <= 600:
        errs.append("width")
    if not 50 <= p.get("height", 50) <= 600:
        errs.append("height")
    if len(p.get("caption", "")) > 1024:
        errs.append("caption")
    low = p["html"]
    for bad in FORBIDDEN:
        if bad in low:
            errs.append("forbidden: " + bad)
    if low.count("<") > 2000:
        errs.append("nodes")
    return errs


def answer_query(q: dict) -> dict:
    """Pure handler: widget_query update object -> answerWidgetQuery params."""
    data = q.get("data", "")
    if not 1 <= len(data.encode()) <= QUERY_DATA_MAX:
        return {"widget_query_id": q["id"], "data": "bad"}
    res = "refreshed" if data == "refresh" else "unknown"
    return {"widget_query_id": q["id"], "data": res[:ANSWER_MAX]}


def api(token, method, params):
    """Delegates to the MP-1337 sink (the only Bot API caller); the token comes from the environment there."""
    import sink  # noqa: PLC0415
    return sink.bot_call(method, params)


def selftest() -> int:
    p = build_send_params(1)
    errs = validate(p)
    assert not errs, errs
    assert answer_query({"id": "1", "data": "refresh"})["data"] == "refreshed"
    assert answer_query({"id": "1", "data": "x" * 65})["data"] == "bad"
    big = [("hi", "t" * 200, "9d")] * 100
    assert len(build_chat_data(big).encode()) <= DATA_MAX
    assert validate({**p, "html": p["html"] + "<iframe>"}), "validator must flag iframe"
    z = len(zlib.compress(p["html"].encode(), 9))
    print("html=%dB zlib=%dB chat_data=%dB" % (len(p["html"].encode()), z,
                                                len(p["chat_data"].encode())))
    print("PASS")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--print", action="store_true", help="print sendWidget params")
    ap.add_argument("--send", metavar="CHAT_ID", help="really send (needs TG_BOT_TOKEN)")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.send:
        tok = os.getenv("TG_BOT_TOKEN")
        if not tok:
            sys.exit("TG_BOT_TOKEN missing")
        p = build_send_params(int(a.send))
        e = validate(p)
        if e:
            sys.exit("invalid: %s" % e)
        print(api(tok, "sendWidget", p).get("ok"))
        return
    print(json.dumps(build_send_params(0), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
