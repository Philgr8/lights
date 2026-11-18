#!/usr/bin/env python3
"""MP-1337 alert sink core: ONE place that talks to the Telegram Bot API for owner alerts.

Stdlib only, no ~/.claude imports, so the same file runs on this PC, on the nuc and in a GitHub Actions runner
(vendor it next to widget_kit.html, widget_demo.py and registry.json; the state dir is a parameter).
`notify.py route <family-id> ...` is a thin CLI over send(); python producers import this module.

Env (names only, values are never printed): TELEGRAM_BOT_TOKEN (@jobwatch777_bot), TELEGRAM_OWNER_CHAT_ID
(fallback TELEGRAM_CHAT_ID for the jobmon cloud secret), TELEGRAM_GROUP_CHAT_ID (Alerts group, caption mirror only).
Delivery rules: registry.json (sound, merge window, destination). Sound has NO daily cap here; the legacy
P0_CAP_DAY lives only in notify.py's old `route telegram` path.
"""
import datetime as dt
import fcntl
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
TG = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")  # the one Bot API literal outside the MP-1338 worker
KYIV = ZoneInfo("Europe/Kyiv")
OQ_WRITER = Path.home() / ".claude" / "bin" / "owner-queue.py"
CAPTION_MAX = 1024
WIDTH = 360


class SinkError(Exception):
    pass


def registry(path=None) -> dict:
    for c in (path, os.environ.get("ALERTS_REGISTRY"), HERE / "registry.json", HERE.parents[1] / "alerts" / "registry.json"):
        if c and Path(c).exists():
            return json.loads(Path(c).read_text())
    raise SinkError("registry.json not found")


def family(rid: str, reg=None) -> dict:
    for f in (reg or registry())["families"]:
        if f["id"] == rid:
            return f
    # MP-1565 (owner 18 Nov 2026): an unknown/new family is never private; it goes to the Alerts group as a plain P0-shaped card
    return {"id": rid, "n": 0, "source": "unregistered", "priority": "UNKNOWN", "destination": "widget", "sound": True,
            "merge_window_min": 0, "mode": "new", "widget": {"label": rid, "height": 260}, "chat_env": "TELEGRAM_GROUP_CHAT_ID"}


def token() -> str:
    t = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not t:
        raise SinkError("TELEGRAM_BOT_TOKEN not set")
    return t


def owner_chat() -> str:
    c = os.environ.get("TELEGRAM_OWNER_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not c:
        raise SinkError("TELEGRAM_OWNER_CHAT_ID not set")
    return c


def family_of(rid: str) -> dict:
    """Family dict for rid; empty/unknown -> the unregistered fallback (Alerts group), see family()."""
    return family(rid or "unregistered")


def family_chat(f: dict) -> str:
    """Per-family chat (owner 18 Nov 2026, MP-1562): registry field `chat_env` names the env variable holding the chat id
    (p0 and eth-forecast -> the Alerts group). No field = the private owner chat (returns "").
    A named but empty variable is a loud error, never a silent fallback to the private chat."""
    name = f.get("chat_env", "")
    if not name:
        return ""
    c = os.environ.get(name, "")
    if not c and name == "TELEGRAM_GROUP_CHAT_ID":  # services without the key store: chat ids are not secrets (same place as extra_chats)
        p = Path.home() / ".config" / "tg_router" / "group_chat_id"
        c = p.read_text().strip() if p.exists() else ""
    if not c:
        raise SinkError(f"{name} not set (family {f.get('id')} routes to it)")
    return c


DUP_METHODS = {"sendMessage", "sendDocument", "sendPhoto", "sendVideo", "sendAnimation", "sendAudio", "sendVoice",
               "sendMediaGroup", "sendWidget", "sendSticker", "sendLocation"}
_NODUP = False  # True inside send(), which fans out itself (each extra chat keeps its own panel state)


def bot_call(method: str, payload: dict, *, files=None, timeout=30, retries=2) -> dict:
    """Owner 12 Nov 2026: every send to the primary owner chat is duplicated to each extra chat (extra_chats()).
    Only the primary call's result/error is returned; extra-chat errors are printed."""
    r = _bot_call_raw(method, payload, files=files, timeout=timeout, retries=retries)
    if _NODUP or method not in DUP_METHODS or str(payload.get("chat_id")) != os.environ.get(
            "TELEGRAM_OWNER_CHAT_ID", os.environ.get("TELEGRAM_CHAT_ID", "")):
        return r
    for c in extra_chats():
        try:
            _bot_call_raw(method, {**{k: v for k, v in payload.items() if k != "reply_to_message_id"}, "chat_id": c},
                          files=files, timeout=timeout, retries=0)
        except Exception as e:
            print(f"[sink] extra chat ..{c[-4:]} {method} failed: {type(e).__name__}", flush=True)
    return r


def _bot_call_raw(method: str, payload: dict, *, files=None, timeout=30, retries=2) -> dict:
    """POST one Bot API method as JSON (or multipart when files is given). Raises SinkError without the URL (it holds the token)."""
    tok = token()
    url = f"{TG}/bot{tok}/{method}"
    if files:
        bnd = "----sink" + uuid.uuid4().hex[:16]
        body = bytearray()
        for k, v in payload.items():
            body += f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, (fn, data) in files.items():
            body += (f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"\r\n'
                     f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + b"\r\n"
        body += f"--{bnd}--\r\n".encode()
        hdr = {"Content-Type": f"multipart/form-data; boundary={bnd}"}
    else:
        body, hdr = json.dumps(payload).encode(), {"Content-Type": "application/json"}
    last = "?"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, body, hdr)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            last = f"HTTP {e.code} {raw[:160]}"
            wait = None
            if e.code == 429:
                m = re.search(r'"retry_after":\s*(\d+)', raw)
                wait = min(int(m.group(1)) if m else 3, 30)
            elif e.code >= 500:
                wait = 2
            if wait is None or attempt == retries:
                break
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = type(e).__name__
            if attempt == retries:
                break
            time.sleep(2)
    raise SinkError(f"{method} failed: {last}")


# ---- state (panel message ids, merge windows) -----------------------------------------------------------------

def _state_dir(sd=None) -> Path:
    d = Path(sd or os.environ.get("ALERTS_STATE_DIR") or Path.home() / ".cache" / "alerts")
    d.mkdir(parents=True, exist_ok=True)
    return d


class _State:
    def __init__(self, sd):
        self.dir = _state_dir(sd)
        self.f = self.dir / "sink_state.json"

    def __enter__(self):
        self.lk = (self.dir / "sink.lock").open("w")
        fcntl.flock(self.lk, fcntl.LOCK_EX)
        self.d = json.loads(self.f.read_text()) if self.f.exists() else {}
        return self

    def save(self):
        tmp = self.f.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d))
        tmp.replace(self.f)

    def __exit__(self, *a):
        self.lk.close()


def _log(sd, rid, action, msg_id, key):
    with (_state_dir(sd) / "sent.tsv").open("a") as f:
        f.write("\t".join([dt.datetime.now(KYIV).isoformat(timespec="seconds"), rid, action, str(msg_id), key]) + "\n")


# ---- widget payload -------------------------------------------------------------------------------------------

def kit_html() -> str:
    return (HERE / "widget_kit.html").read_text(encoding="utf-8")


def cdata(d: dict) -> str:
    """Compact chat_data; trims list rows / timelines until it fits 1024 bytes."""
    for _ in range(12):
        s = json.dumps(d, separators=(",", ":"), ensure_ascii=False)
        if len(s.encode()) <= 1024:
            return s
        for k in ("l", "t", "k"):
            if len(d.get(k) or []) > 1:
                d[k] = d[k][:-1]
                break
        else:
            d["w"] = (d.get("w") or "")[:max(0, len(d.get("w") or "") - 40)]
    raise SinkError("chat_data > 1024 B after trimming")


def build_params(f: dict, data: dict, caption: str, chat: str, sound: bool, preview=False) -> dict:
    d = dict(data)
    d.setdefault("f", f["widget"]["label"])
    d.setdefault("a", dt.datetime.now(KYIV).strftime("%H:%M"))
    if preview:
        d["a"] = "Preview"
    interactive = bool(d.get("bg")) or any(b[2] != "l" for b in d.get("x", []))
    p = {"chat_id": chat, "html": kit_html(), "width": WIDTH, "height": f["widget"]["height"], "chat_data": cdata(d),
         "allow_queries": interactive, "caption": ((("Preview: " if preview else "") + caption).strip() or f["widget"]["label"])[:CAPTION_MAX],
         "disable_notification": not sound}
    sys.path.insert(0, str(HERE))
    import widget_demo  # noqa: PLC0415 (size gate: html <= 32768, zlib <= 4096, chat_data <= 1024)
    errs = widget_demo.validate(p)
    if errs:
        raise SinkError("size gate: " + "; ".join(errs))
    return p


# ---- text render (registry "render": "text" | "widget"; per-family "render" overrides) ----------------------

#: first token of a widget request -> the MP-1338 callback prefix the router/worker will own. UNCALIBRATED mapping:
#: the router (branch R) decides the final grammar; the original token is kept after the prefix so nothing is lost.
CB_PREFIX = {"heur": "heur", "smm": "smm", "lt": "lights", "oq": "stall", "nfy": "nfy",
             "sen": "nfy", "wal": "nfy", "tcb": "nfy"}
CHIP = {"g": "\U0001F7E2", "r": "\U0001F534", "a": "\U0001F7E0", "b": "\u26AA"}   # status marks only, no blue / gold


def render_of(reg: dict, f: dict) -> str:
    r = f.get("render") or reg.get("render") or "text"
    return r if r in ("text", "widget") else "text"


def now_hm() -> str:
    """Time for line 1 of every message: HH:MM Kyiv (owner standard 11 Nov 2026)."""
    return dt.datetime.now(KYIV).strftime("%H:%M")


def ru_n(n: int, one: str, few: str, many: str) -> str:
    """Russian plural: 1 совпадение, 2 совпадения, 5 совпадений."""
    n10, n100 = abs(n) % 10, abs(n) % 100
    return one if n10 == 1 and n100 != 11 else few if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else many


def _esc(x) -> str:
    return html.escape(str(x), quote=False)


def callback_data(req: str) -> str:
    head, _, rest = req.partition(":")
    pre = CB_PREFIX.get(head)
    cb = f"{pre}:{req}" if pre == "nfy" else f"{pre}:{rest}" if pre else req
    return cb.encode()[:64].decode("utf-8", "ignore")


MARK = {"g": "\U0001F7E9", "r": "\U0001F7E5", "a": "\U0001F7E7", "b": "\u2B1C"}   # status squares only (P0 red, warn orange, ok green, info black)
#: family id -> (monochrome glyph, CAPS English tag); MP-1563 cyberpunk text cards
TAGS = {"p0": ("\u231D", "P0 // ALARM"), "lights": ("\u2301", "LIGHTS // GRID"), "jobs": ("\u25B8", "JOBS // FEED"),
        "wallet": ("\u26D3", "WALLET // MOVE"), "smm-approval": ("\u25E2", "SMM // APPROVAL"), "smm-alert": ("\u25E4", "SMM // ALERT"),
        "tcb": ("\u2593", "TCB // WATCH"), "heuristics": ("\u27E6\u27E7", "HEUR // LAUNCH"), "owner-queue": ("\u2592", "OQ // QUEUE"),
        "sensor-red": ("\u2591", "SENSOR // RED"), "eth-forecast": ("\u25B2", "ETH // FORECAST"),
        "opps": ("\u25C8", "OPPS // FEED")}
BSTYLE = {"p": "success", "d": "danger", "s": "primary"}   # Bot API button style; MP-1577 phone-width cards
BTN = {"p": "\u2713", "s": "\u23F8", "d": "\u2715", "l": "\u2197"}


def bar(pct, n: int = 10) -> str:
    """Number as a bar: 62 -> ▰▰▰▰▰▰▱▱▱▱ 62%."""
    p = max(0, min(100, int(round(float(pct)))))
    k = round(p * n / 100)
    return "\u25B0" * k + "\u25B1" * (n - k) + f" {p}%"


def _one(x, n: int = 44) -> str:
    t = " ".join(str(x).split())
    return t if len(t) <= n else t[: n - 1] + "\u2026"


def _pre(lines, lang: str = "ini") -> str:
    return f'<pre><code class="language-{lang}">' + "\n".join(_esc(x) for x in lines) + "</code></pre>"


def render_text(f: dict, d: dict, caption: str, preview: bool) -> tuple[str, dict | None]:
    """MP-1577 phone-width cards (v2c): header `mark <b>TAG</b> <i>chip</i>` (<= 22 visible chars); key rows k as one ini <pre>
    (every line <= 22 chars, long values move to <code> lines); h/w/u/l/schedule as <code> lines, first 3 shown, the rest in an
    expandable blockquote; optional css <pre> bar; no frames. Data fields: c chip, h head, w sub, u body, k key rows, l list rows,
    t timeline, x buttons; optional tag, bar [pct], bar_lbl, arrow. Button kinds: p/s/d callback (success/primary/danger), l url, c copy_text (no icon)."""
    chip = d.get("c") or ["b", ""]
    fid = str(f.get("id") or "")
    tag = str(d.get("tag") or TAGS.get(fid, ("", fid.upper() or "ALERT"))[1]).split(" // ")[0]
    tag = ("TEST " if preview else "") + tag
    ctext = str(chip[1]) if len(chip) > 1 and chip[1] else ""
    tag = _one(tag, 19)
    head = f"{MARK.get(chip[0], MARK['b'])} <b>{_esc(tag)}</b>"
    if ctext and 19 - len(tag) >= 2:
        head += f" <i>{_esc(_one(ctext, 19 - len(tag)))}</i>"
    out = [head]
    pre_rows, codes, sched = [], [], []
    wv = str(d.get("w") or "")
    if fid == "lights" and re.search(r"(?m)^\d{2}\.\d{2}\s*$", wv):  # MP-1566: each outage window on its own line, grouped by date
        notes = []
        for blk in wv.split("\n\n"):
            ls = [x.strip() for x in blk.split("\n") if x.strip()]
            if ls and re.fullmatch(r"\d{2}\.\d{2}", ls[0]):
                sched.append(ls[0])
                sched += ["\u2003" + x for x in ls[1:]]
            elif ls:
                notes.append(" ".join(ls))
        d = dict(d, w="; ".join(notes))
    ks = [(str(k[0]), str(k[1])) for k in d.get("k") or [] if len(k) > 1 and k[1]]
    kw = min(8, max((len(_one(a, 8)) for a, _ in ks), default=0))
    for a, v in ks[:5]:
        v = " ".join(v.split())
        if kw + 3 + len(v) <= 22:
            pre_rows.append(f"{_one(a, 8):<{kw}} = {v}")
        else:
            codes.append(f"{_one(a, 8)} {_one(v, 40)}")
    for key in ("h", "w", "u"):
        if d.get(key):
            v = " ".join(x for x in str(d[key]).split("\n") if x.strip()) if key == "w" and "\n\n" in str(d[key]) else str(d[key]).split("\n")[0]
            codes.append(_one(v, 56 if key == "w" else 44))
    for r in d.get("l") or []:
        codes.append(_one(r[1], 60) + (" " + _one(r[2], 10) if len(r) > 2 and r[2] else ""))
    for t in d.get("t") or []:
        sched.append(_one(t[0], 12))
        sched += ["\u2003" + f"{int(a):02d}:{int(a % 1 * 60):02d}-{int(b):02d}:{int(b % 1 * 60):02d}" for a, b, *_ in t[2]] or ["\u2003clear"]
    if d.get("arrow"):
        codes.insert(0, ("\u25B2" if d["arrow"] == "up" else "\u25BC") + (" " + _one(d.get("arrow_txt", ""), 30) if d.get("arrow_txt") else ""))
    codes += sched
    # MP-1580 (owner 18 Nov 2026): at most 4 visible lines (head + key numbers); everything else under the expandable cut
    budget = 3 - ((1 + bool(d.get("bar_lbl"))) if d.get("bar") is not None else 0)
    budget = max(budget, 1)
    shown_pre = pre_rows[:budget]
    hidden = [r.replace(" = ", " ", 1) for r in pre_rows[budget:]]
    if shown_pre:
        out.append(_pre(shown_pre))
    nc = max(budget - len(shown_pre), 0)
    out += [f"<code>{_esc(c)}</code>" for c in codes[:nc]]
    hidden += codes[nc:]
    if hidden:
        out.append("<blockquote expandable>" + "\n".join(_esc(c) for c in hidden) + "</blockquote>")
    if d.get("bar") is not None:
        out.append(_pre([_one(d.get("bar_lbl"), 22)] * bool(d.get("bar_lbl")) + [bar(d["bar"])], "css"))
    kb, row = [], []
    for b in d.get("x") or []:
        st = b[2] if len(b) > 2 else "s"
        if st == "c":
            btn = {"text": str(b[0])[:24], "copy_text": {"text": str(b[1])[:256]}}
        else:
            ic = "↻" if str(b[0]).lower().startswith(("refresh", "обновить")) else BTN.get(st, chr(0x25B8))
            btn = {"text": f"{ic} {str(b[0])[:24]}"}
            if st == "l":
                btn["url"] = b[1]
            else:
                btn["callback_data"] = callback_data(b[1])
                if BSTYLE.get(st):
                    btn["style"] = BSTYLE[st]
        row.append(btn)
        if len(row) == 3:
            kb.append(row); row = []
    if row:
        kb.append(row)
    return "\n".join(out)[:4000], ({"inline_keyboard": kb} if kb else None)


def text_params(f: dict, d: dict, caption: str, chat: str, sound: bool, preview=False) -> dict:
    text, kb = render_text(f, d, caption, preview)
    p = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_notification": not sound,
         "link_preview_options": {"is_disabled": True}, "protect_content": f.get("id") in ("wallet", "smm-approval")}
    if kb:
        p["reply_markup"] = kb
    return p


def _merge(rid: str, prev: dict, new: dict) -> dict:
    if rid == "tcb":
        out = dict(new)
        out["l"] = ((prev.get("l") or []) + (new.get("l") or []))[-4:]
        n = int(prev.get("n", 1)) + 1
        out["n"] = n
        out["c"] = ["a", f"{n} " + ru_n(n, "совпадение", "совпадения", "совпадений")]
        out["h"] = f"{n} " + ru_n(n, "совпадение", "совпадения", "совпадений") + " за 5 минут"
        return out
    return new  # lights: the later schedule replaces the earlier one inside the window


def _mirror(reg: dict, caption: str) -> None:
    """Alerts group: caption-only line until alerts_group.retire_after (config flag, nothing deleted)."""
    g = reg.get("alerts_group") or {}
    chat = os.environ.get("TELEGRAM_GROUP_CHAT_ID", "")
    if not chat or g.get("mode") != "caption-only" or dt.datetime.now(KYIV).date().isoformat() > g.get("retire_after", ""):
        return
    try:
        bot_call("sendMessage", {"chat_id": chat, "text": caption[:1000], "disable_notification": True}, retries=0)
    except SinkError:
        pass


def extra_chats() -> list[str]:
    """Second owner account(s) (owner 11 Nov 2026): TELEGRAM_EXTRA_CHAT_IDS (comma list) or ~/.config/tg_router/extra_chats."""
    v = os.environ.get("TELEGRAM_EXTRA_CHAT_IDS", "")
    if not v:
        p = Path.home() / ".config" / "tg_router" / "extra_chats"
        v = p.read_text() if p.exists() else ""
    return [c for c in re.split(r"[,\s]+", v) if c.strip("-").isdigit() and c != owner_chat()]


def send(rid: str, data: dict, caption: str = "", *, key: str = "", preview: bool = False, state_dir=None,
         mode: str = "") -> dict:
    """Primary owner chat first (its error propagates), then each extra chat with its own panel/merge slot (errors only printed)."""
    global _NODUP
    _NODUP = True
    try:
        r = _send_one(rid, data, caption, key=key, preview=preview, state_dir=state_dir, mode=mode)
    finally:
        _NODUP = False
    if not preview and not family(rid).get("chat_env"):  # a group family is MOVED, not copied to extra chats
        for c in extra_chats():
            try:
                _send_one(rid, data, caption, key=key, state_dir=state_dir, mode=mode, _chat=c)
            except Exception as e:
                print(f"[sink] extra chat {c} failed: {type(e).__name__}", flush=True)
    return r


def _send_one(rid: str, data: dict, caption: str = "", *, key: str = "", preview: bool = False, state_dir=None,
              mode: str = "", _chat: str = "") -> dict:
    """Deliver one alert of family rid. Returns {"action": sent|merged|edited|unchanged|queued, "msg_id": ...}.
    mode: "" = the family default; "panel" = the pinned live panel (edit in place, nothing sent when unchanged)."""
    reg = registry()
    f = family(rid, reg)
    if f["destination"] == "8765#q":
        return queue(rid, key or rid, caption or data.get("h", rid), state_dir=state_dir, preview=preview)
    if f["destination"] != "widget":
        return {"action": "none"}
    chat = _chat or family_chat(f) or owner_chat()
    sound = bool(f["sound"]) and mode != "panel"
    text_mode = render_of(reg, f) == "text"
    M_SEND, M_EDIT = ("sendMessage", "editMessageText") if text_mode else ("sendWidget", "editMessageWidgetData")

    def build(dd):
        return text_params(f, dd, caption, chat, sound, preview) if text_mode else build_params(f, dd, caption, chat, sound, preview)

    def cdk(pp, dd):   # what "unchanged" and the merge window compare / store
        return json.dumps(dd, sort_keys=True, ensure_ascii=False) + json.dumps(pp.get("reply_markup", ""), sort_keys=True) if text_mode else pp["chat_data"]

    def edit_p(pp, mid):
        if text_mode:
            return {**{k: v for k, v in pp.items() if k not in ("disable_notification", "protect_content")}, "message_id": mid}
        return {"chat_id": chat, "message_id": mid, "chat_data": pp["chat_data"]}

    p = build(data)
    slot = f"{rid}:panel" if mode == "panel" else rid
    if _chat:
        slot += f"@{_chat}"
    now = time.time()
    with _State(state_dir) as st:
        prev = st.d.get(slot)
        if mode == "panel":
            if prev and prev["cd"] == cdk(p, data):
                return {"action": "unchanged", "msg_id": prev["id"]}
            if prev:
                try:
                    bot_call(M_EDIT, edit_p(p, prev["id"]))
                    prev.update(cd=cdk(p, data), ts=now)
                    st.save()
                    _log(state_dir, rid, "edited", prev["id"], key)
                    return {"action": "edited", "msg_id": prev["id"]}
                except SinkError as e:
                    if "not modified" in str(e).lower():
                        return {"action": "unchanged", "msg_id": prev["id"]}
                    # message deleted or unreachable: fall through and send a fresh panel
        elif f["merge_window_min"] and prev and not preview and now - prev["ts"] < f["merge_window_min"] * 60:
            merged = _merge(rid, json.loads(prev["dj"] if text_mode else prev["cd"]), data)
            p2 = build(merged)
            try:
                bot_call(M_EDIT, edit_p(p2, prev["id"]))
                st.d[slot] = {"id": prev["id"], "ts": prev["ts"], "cd": cdk(p2, merged), "dj": json.dumps(merged)}
                st.save()
                _log(state_dir, rid, "merged", prev["id"], key)
                return {"action": "merged", "msg_id": prev["id"]}
            except SinkError:
                pass  # the earlier message is gone: send a new one
        r = bot_call(M_SEND, p)
        mid = (r.get("result") or {}).get("message_id")
        if not r.get("ok") or mid is None:
            raise SinkError(M_SEND + " returned not ok")
        if not preview:
            st.d[slot] = {"id": mid, "ts": now, "cd": cdk(p, data), "dj": json.dumps(data)}
            st.save()
        _log(state_dir, rid, "preview" if preview else "sent", mid, key)
    if not preview and mode != "panel" and not _chat and not f.get("chat_env"):
        _mirror(reg, caption or data.get("h", ""))
    print(f"sent message_id={mid}", flush=True)
    return {"action": "sent", "msg_id": mid}


def queue(rid: str, key: str, text: str, *, state_dir=None, preview=False) -> dict:
    """destination 8765#q: the owner-queue writer (owner-queue.py add), the same store the 8765 page reads."""
    f = family(rid)
    slug = re.sub(r"[^a-z0-9-]+", "-", f"{rid}-{key}".lower()).strip("-")[:63]
    if not OQ_WRITER.exists():
        raise SinkError("owner-queue.py not found on this machine (queue destinations run where the owner queue lives)")
    if preview:
        return {"action": "queued-dry", "key": slug}
    r = subprocess.run([sys.executable, "-I", str(OQ_WRITER), "add", "--key", slug, "--why", f["oq_why"], "--stdin", "--session", "alerts-sink"],
                       input=" ".join(text.split())[:600], text=True, capture_output=True, timeout=60)
    if r.returncode:
        raise SinkError(f"owner-queue add rc {r.returncode}: {r.stderr.strip()[-200:]}")
    _log(state_dir, rid, "queued", slug, key)
    return {"action": "queued", "key": slug}


def send_text(text: str, *, silent: bool = True, parse_mode: str = "", reply_to=None, family: str = "") -> dict:
    """Plain text. MP-1565: routed by the family chat field; no/unknown family = the Alerts group, only lights + jobs are private."""
    p = {"chat_id": family_chat(family_of(family)) or owner_chat(), "text": text[:4000], "disable_notification": silent, "disable_web_page_preview": True}
    if parse_mode:
        p["parse_mode"] = parse_mode
    if reply_to:
        p["reply_to_message_id"] = reply_to
    r = bot_call("sendMessage", p)
    if r.get("ok"):
        print(f"sent message_id={(r.get('result') or {}).get('message_id')}", flush=True)
    return r


def send_document(path: str, caption: str = "", parse_mode: str = "", reply_to=None, family: str = "") -> dict:
    p = {"chat_id": family_chat(family_of(family)) or owner_chat(), "caption": caption[:CAPTION_MAX], "disable_notification": True}
    if parse_mode:
        p["parse_mode"] = parse_mode
    if reply_to:
        p["reply_to_message_id"] = reply_to
    return bot_call("sendDocument", p, files={"document": (os.path.basename(path), Path(path).read_bytes())}, timeout=60)


def whoami() -> int:
    """Preflight: the bot behind TELEGRAM_BOT_TOKEN must be the registry bot; prints username and set/missing only."""
    reg = registry()
    try:
        u = bot_call("getMe", {}, retries=0)["result"]["username"]
    except SinkError as e:
        print("bot: unreachable", e)
        return 2
    chat = bool(os.environ.get("TELEGRAM_OWNER_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID"))
    print(f"bot: @{u} (registry: @{reg['bot']}); owner chat env: {'set' if chat else 'missing'}")
    return 0 if u == reg["bot"] and chat else 2


if __name__ == "__main__":
    if sys.argv[1:] == ["whoami"]:
        sys.exit(whoami())
    sys.exit("usage: sink.py whoami   (send via: notify.py route <family-id> --json -)")
