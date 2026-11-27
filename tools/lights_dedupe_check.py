#!/usr/bin/env python3
"""cases by plan/owner, 2026-11-27, incident MP-2325

Replay of the 27 Nov alerts (16:16, 16:35, 16:56, 17:15 Kyiv) through tick() -> send() -> sink.send(), dry: only the
Telegram HTTP layer is stubbed, state is carried between ticks in a temp dir. The consensus alternates A,B,A,B as
recorded on branch lights-state (A/B differ only in an ended half hour). The state before the first tick lacks the
evening window, so the first payload is a real visible change. Expected: exactly 1 send (at the first tick), rc 0.
"""
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
A = "000000000000000011111111111111100000011111111000"
B = "000000000000000011111111111111000000011111111000"
PRIOR = "000000000000000011111111111111000000000000000000"
DAY = "2026-11-27"
TICKS = [("16:16", A), ("16:35", B), ("16:56", A), ("17:15", B)]

tmp = tempfile.mkdtemp(prefix="lights-dedupe-")
for k in [k for k in os.environ if k.startswith("TELEGRAM_")]:
    del os.environ[k]
os.environ.update(HOME=tmp, LIGHTS_STATE=tmp, TELEGRAM_BOT_TOKEN="dry", TELEGRAM_OWNER_CHAT_ID="1",
                  TELEGRAM_API_BASE="http://127.0.0.1:9")
sys.path.insert(0, str(ROOT))
import lights  # noqa: E402
import sink  # noqa: E402
import types  # noqa: E402

calls = []


def fake_api(method, payload, **kw):
    calls.append(method)
    return {"ok": True, "result": {"message_id": len(calls), "username": sink.registry()["bot"]}}


sink._bot_call_raw = fake_api
Path(tmp, "state.json").write_text(json.dumps({
    "sched": {DAY: PRIOR}, "sent": {f"morning:{DAY}": f"{DAY}T09:15:00+02:00"}, "fail": 0,
    "card_key": f"0|{PRIOR}|"}))

per_tick = []
for hhmm, slots in TICKS:
    at = dt.datetime.fromisoformat(f"{DAY}T{hhmm}:00+02:00").astimezone(lights.KYIV)
    lights.now = lambda: at
    # the sink's 10 min merge window must see the replayed clock, not the 1 s the replay takes
    sink.time = types.SimpleNamespace(time=at.timestamp, sleep=lambda s: None)
    lights.fetch_all = lambda s=slots: ({"alerts": {DAY: s}}, {})
    before = len(calls)
    if lights.tick(False):
        sys.exit("[FATAL] tick failed")
    per_tick.append([m for m in calls[before:] if m.startswith("send")])

total = sum(map(len, per_tick))
print(f"[dedupe] sends per tick: {[len(x) for x in per_tick]} total {total} (expected [1, 0, 0, 0])")
failed = [len(x) for x in per_tick] != [1, 0, 0, 0]

# MP-2340 (cases by owner/base, 27 Nov 2026): any update = a NEW message (mode != panel), no update = nothing.
real_send, modes = sink.send, []


def spy(rid, data, *a, **kw):
    modes.append(kw.get("mode", ""))
    return real_send(rid, data, *a, **kw)


sink.send = spy
ON = "0" * 48
TM = "0" * 20 + "1" * 8 + "0" * 20            # 10:00-14:00
TD = "0" * 36 + "1" * 4 + "0" * 8             # 18:00-20:00
NEXT = "2026-11-28"


def run(name, first_state, ticks):
    """ticks: (iso time, {date: slots}, expected number of new messages)."""
    global failed
    box = Path(tmp, name)
    box.mkdir()
    lights.STATE_DIR, lights.STATE, lights.LOG = box, box / "state.json", box / "lights.log"
    lights.STATE.write_text(json.dumps(first_state))
    for iso, cons, want in ticks:
        at = dt.datetime.fromisoformat(iso + ":00+02:00").astimezone(lights.KYIV)
        lights.now = lambda: at
        sink.time = types.SimpleNamespace(time=at.timestamp, sleep=lambda s: None)
        lights.fetch_all = lambda c=cons: ({"alerts": c}, {})
        before, m0 = len(calls), len(modes)
        rc = lights.tick(False)
        got = calls[before:]
        sends = sum(x.startswith("send") for x in got)
        edits = sum(x.startswith("edit") for x in got)
        ok = rc == 0 and sends == want and not edits and "panel" not in modes[m0:]
        failed = failed or not ok
        print(f"[{name}] {iso[5:]} sends {sends} (expected {want}) edits {edits} {'ok' if ok else 'FAIL'}")


def state(day, td):
    return {"sched": {day: td}, "sent": {}, "fail": 0, "card_key": f"0|{td}|", "card_day": day}


# (a) tomorrow published, old=None; (c) identical ticks, also at morning/evening times and across midnight
run("published", state(DAY, ON), [
    (f"{DAY}T15:00", {DAY: ON}, 0),
    (f"{DAY}T15:20", {DAY: ON, NEXT: TM}, 1),
    (f"{DAY}T15:25", {DAY: ON, NEXT: TM}, 0),
    (f"{DAY}T20:15", {DAY: ON, NEXT: TM}, 0),
    (f"{NEXT}T00:15", {NEXT: TM}, 0),
    (f"{NEXT}T09:15", {NEXT: TM}, 0),
    (f"{NEXT}T10:15", {NEXT: TM}, 1),          # (b) on -> off
    (f"{NEXT}T10:20", {NEXT: TM}, 0),
    (f"{NEXT}T14:15", {NEXT: TM}, 1),          # (b) off -> on
])
# a schedule change minutes after a flip is a new message too (no merge-window edit)
run("flip", state(DAY, TD), [
    (f"{DAY}T17:55", {DAY: TD}, 0),
    (f"{DAY}T18:01", {DAY: TD}, 1),
    (f"{DAY}T18:05", {DAY: TD[:42] + "11" + TD[44:]}, 1),
])
sys.exit(1 if failed else 0)
