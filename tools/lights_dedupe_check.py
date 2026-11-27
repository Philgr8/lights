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
sys.exit(0 if [len(x) for x in per_tick] == [1, 0, 0, 0] else 1)
