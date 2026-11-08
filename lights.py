#!/usr/bin/env python3
"""Power-outage watcher: DTEK Odesa schedule for one queue -> Telegram private chat. Stdlib only, no LLM.

Sources (public pages, parsed by script): bezsvitla.com.ua, alerts.energy (oblenergo entry), svitlo.live.
Each source yields {date: 48 half-hour slots, '1' = no power}; the consensus is a slot-wise majority.

  lights.py tick [--dry]   one pass (systemd timer, every 30 min): changes, reminders, morning summary
  lights.py show           print each source and the consensus, send nothing
  lights.py chatid         read getUpdates, store the private chat id of whoever sent /start
  lights.py test           send one test line

Env: TELEGRAM_BOT_TOKEN + TELEGRAM_GROUP_CHAT_ID ("Claude Alerts", ALERT_* names win; never printed), LIGHTS_QUEUE (default 1.1), LIGHTS_STATE (state dir).
"""
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
QUEUE = os.getenv("LIGHTS_QUEUE", "1.1")
STATE_DIR = Path(os.getenv("LIGHTS_STATE", str(Path.home() / ".local/state/lights")))
STATE = STATE_DIR / "state.json"
LOG = STATE_DIR / "lights.log"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
WARN_OFF_MIN = 30     # reminder before an outage
WARN_ON_MIN = 30      # reminder before power returns (>= TICK_MIN or it is skipped)
GAP_MIN = 70          # tick gap that means the nuc itself was down (> 2 ticks)
TICK_MIN = 30         # lights.timer period (owner 7 Nov 2026: 30 min, was 5)
HOST = os.getenv("LIGHTS_HOST", "Nuc")  # who runs the ticks: Nuc (systemd) or GitHub (Actions cron, owner 8 Nov 2026)
MORNING = dt.time(9, 0)
EVENING = dt.time(20, 0)
FAIL_ALERT = 3        # consecutive all-source failures before one alert

URLS = {
    "bezsvitla": "https://bezsvitla.com.ua/odeska-oblast/cornomorsk",
    "alerts": "https://alerts.energy/odeska-oblast",
    "svitlo": "https://svitlo.live/chornomorsk",
    "tg": "https://t.me/s/chornomorsk_svitlo",
}


def now() -> dt.datetime:
    fake = os.getenv("LIGHTS_NOW")  # dry-run replay only, e.g. 2026-11-07T15:05
    return dt.datetime.fromisoformat(fake).replace(tzinfo=KYIV) if fake else dt.datetime.now(KYIV)


def log(msg: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{now():%F %T} {msg}\n")


def get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "uk,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


# ---------- parsers: each returns {iso_date: 48-char '0'/'1' string} for published days ----------

def slots_from_intervals(items) -> str:
    s = ["0"] * 48
    for it in items:
        if str(it.get("state", "")).upper() != "OFF":
            continue
        a = [int(x) for x in it["from"].split(":")]
        b = [int(x) for x in it["to"].split(":")]
        for i in range((a[0] * 60 + a[1]) // 30, (b[0] * 60 + b[1]) // 30):
            s[i] = "1"
    return "".join(s)


def parse_bezsvitla(page: str) -> dict:
    q = QUEUE.replace(".", "-")
    for card in page.split('<article class="bz-schedule-card"')[1:]:
        if f'href="/odeska-oblast/cherha-{q}"' not in card:
            continue
        m_date = re.search(r'data-schedule-date="(\d{4}-\d\d-\d\d)"', card)
        m_today = re.search(r'data-schedule-intervals="([^"]*)"', card)
        m_tom = re.search(r'data-schedule-tomorrow="([^"]*)"', card)
        if not (m_date and m_today):
            continue
        d = dt.date.fromisoformat(m_date.group(1))
        out = {}
        today = json.loads(html.unescape(m_today.group(1)) or "[]")
        if today:
            out[d.isoformat()] = slots_from_intervals(today)
        tom = json.loads(html.unescape(m_tom.group(1)) or "[]") if m_tom else []
        if isinstance(tom, dict):
            tom = tom.get("intervals", [])
        if tom:
            out[(d + dt.timedelta(days=1)).isoformat()] = slots_from_intervals(tom)
        return out
    raise ValueError("bezsvitla: queue card not found")


def hours24_to_slots(codes) -> str:
    # alerts.energy codes: 0 power, 1 off all hour, 2 off first half, 3 off second half
    m = {0: "00", 1: "11", 2: "10", 3: "01"}
    return "".join(m.get(int(c), "11") for c in codes)


def parse_alerts(page: str) -> dict:
    m = re.search(r'<script id="ng-state" type="application/json">(.*?)</script>', page, re.S)
    if not m:
        raise ValueError("alerts: ng-state missing")
    found = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("initiator") == "odeska_oblast_oblenergo" and o.get("queue") == QUEUE and "today" in o:
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(json.loads(m.group(1)))
    if not found:
        raise ValueError("alerts: oblenergo queue entry missing")
    e = found[0]
    out = {}
    for k in ("today", "tomorrow"):
        if len(e.get(k) or []) == 24 and e.get(f"{k}Date"):
            out[e[f"{k}Date"]] = hours24_to_slots(e[k])
    return out


def parse_svitlo(page: str) -> dict:
    # Two tables per queue (today, tomorrow); a row = 24 <td class="on|off|f4|f5|noinfo">.
    # f4 = first half-hour off, f5 = second half-hour off (checked against the other two sources).
    m = {"on": "00", "off": "11", "f4": "10", "f5": "01"}
    out = {}
    i = page.find(f'id="chergra{QUEUE}"')
    if i < 0:
        raise ValueError("svitlo: queue block missing")
    j = page.find('id="chergra', i + 10)
    block = page[i:j if j > 0 else i + 40000]
    for part in block.split('<table class="graph"')[1:3]:
        md = re.search(r"(\d\d)\.(\d\d)\.(\d{4})", part)
        cells = re.findall(r'<td class="([a-z0-9]+)"', part)[:24]
        if md and len(cells) == 24 and all(c in m for c in cells):
            out[f"{md.group(3)}-{md.group(2)}-{md.group(1)}"] = "".join(m[c] for c in cells)
    return out


UA_MONTHS = {m: i for i, m in enumerate(
    "січня лютого березня квітня травня червня липня серпня вересня жовтня листопада грудня".split(), 1)}


def parse_tg(page: str) -> dict:
    """Public channel t.me/s/chornomorsk_svitlo (web preview, no login), added 8 Nov 2026 (owner).
    Posts: '... графік відключень ... на 8 листопада' + lines 'з 2:00 до 6:00'. No queue named in posts; checked
    against our queue 1.1 on 7 and 8 Nov: same intervals, posted 13-20 min before the sites updated.
    Later posts for the same date ('Оновлення') override earlier ones; the page lists posts oldest first."""
    out = {}
    for block in page.split('class="tgme_widget_message_wrap')[1:]:
        mt = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', block, re.S)
        mp = re.search(r'<time datetime="(\d{4})-(\d\d)', block)
        if not mt or not mp:
            continue
        text = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<br\s*/?>", "\n", mt.group(1))))
        md = re.search(r"графік відключень.*?на\s+(\d{1,2})\s+([а-яіїє]+)", text, re.S | re.I)
        if not md or md.group(2).lower() not in UA_MONTHS:
            continue
        year, pmonth, month = int(mp.group(1)), int(mp.group(2)), UA_MONTHS[md.group(2).lower()]
        if pmonth == 12 and month == 1:
            year += 1
        s = ["0"] * 48
        for a_h, a_m, b_h, b_m in re.findall(r"з\s+(\d{1,2}):(\d\d)\s+до\s+(\d{1,2}):(\d\d)", text):
            a, b = int(a_h) * 2 + int(a_m) // 30, int(b_h) * 2 + int(b_m) // 30
            if b <= a:  # 'до 0:00' / 'до 24:00' = end of day
                b = 48
            for i in range(a, min(b, 48)):
                s[i] = "1"
        out[dt.date(year, month, int(md.group(1))).isoformat()] = "".join(s)
    return out


PARSERS = {"bezsvitla": parse_bezsvitla, "alerts": parse_alerts, "svitlo": parse_svitlo, "tg": parse_tg}


def fetch_all() -> tuple[dict, dict]:
    res, errs = {}, {}
    for name, fn in PARSERS.items():
        try:
            res[name] = fn(get(URLS[name]))
        except Exception as e:  # one broken source must not stop the others
            errs[name] = f"{type(e).__name__}: {e}"[:200]
    return res, errs


def consensus(res: dict) -> tuple[dict, dict]:
    """Slot-wise majority per date; returns ({date: slots}, {date: 'agree/total'})."""
    dates = sorted({d for r in res.values() for d in r})
    out, agree = {}, {}
    for d in dates:
        vals = [r[d] for r in res.values() if d in r]
        if len(vals) == 2 and vals[0] != vals[1]:
            # two sources disagree: prefer the oblenergo copy (alerts), else bezsvitla
            pick = res.get("alerts", {}).get(d) or res.get("bezsvitla", {}).get(d) or vals[0]
            out[d] = pick
        else:
            # majority per slot; a tie (2 of 4 since the tg source, 8 Nov 2026) goes to alerts, else bezsvitla
            ref = res.get("alerts", {}).get(d) or res.get("bezsvitla", {}).get(d) or vals[0]
            ones = [sum(v[i] == "1" for v in vals) for i in range(48)]
            out[d] = "".join("1" if 2 * n > len(vals) else "0" if 2 * n < len(vals) else ref[i]
                             for i, n in enumerate(ones))
        agree[d] = f"{sum(v == out[d] for v in vals)}/{len(vals)}"
    return out, agree


# ---------- schedule helpers ----------

def hm(slot: int) -> str:
    return f"{slot // 2:02d}:{(slot % 2) * 30:02d}" if slot < 48 else "24:00"


def intervals(slots: str) -> list[tuple[int, int]]:
    out, i = [], 0
    while i < 48:
        if slots[i] == "1":
            j = i
            while j < 48 and slots[j] == "1":
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


def describe(slots: str) -> str:
    iv = intervals(slots)
    if not iv:
        return "без отключений"
    total = sum(b - a for a, b in iv) / 2
    return ", ".join(f"{hm(a)}-{hm(b)}" for a, b in iv) + f" ({total:g} ч без света)"


def column(slots: str) -> str:
    """Outage periods one per line, no totals: the schedule-change message (owner 7 Nov 2026)."""
    iv = intervals(slots)
    return "\n".join(f"{hm(a)}-{hm(b)}" for a, b in iv) if iv else "без отключений"


def abs_outages(sched: dict, today: dt.date) -> list[tuple[dt.datetime, dt.datetime]]:
    """Outages as Kyiv datetimes over yesterday..tomorrow, merged across midnight."""
    out = []
    for k in (-1, 0, 1):
        d = today + dt.timedelta(days=k)
        s = sched.get(d.isoformat())
        if not s:
            continue
        base = dt.datetime.combine(d, dt.time(0), KYIV)
        for a, b in intervals(s):
            st, en = base + dt.timedelta(minutes=30 * a), base + dt.timedelta(minutes=30 * b)
            if out and out[-1][1] == st:
                out[-1] = (out[-1][0], en)
            else:
                out.append((st, en))
    return out


# ---------- state and Telegram ----------

def load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"sched": {}, "sent": {}, "fail": 0}


def save(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    os.replace(tmp, STATE)  # atomic: power can drop mid-write


def _alert_token() -> str:
    return os.getenv("TELEGRAM_ALERT_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")


def _alert_chat() -> str:
    return os.getenv("TELEGRAM_ALERT_CHAT_ID") or os.getenv("TELEGRAM_GROUP_CHAT_ID", "")


def tg(method: str, **params) -> dict:
    tok = _alert_token()
    if not tok:
        sys.exit("[FATAL] TELEGRAM_BOT_TOKEN missing")
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/{method}", data=data, timeout=30) as r:
        return json.load(r)


def send(st: dict, text: str, dry: bool) -> bool:
    if dry:
        print("WOULD SEND:", text)
        return True
    # Owner 7 Nov 2026: same "Claude Alerts" group as notify.py / usage_alerts (metime); same chat resolution as
    # notify.creds(). Sent directly, not via `notify.py route telegram`: its P0 cap (5/day, one key per 24 h)
    # would drop the 4-8 outage reminders of a normal day. Dedupe is this script's own `sent` keys.
    chat = os.getenv("LIGHTS_CHAT_ID") or _alert_chat()
    if not chat:
        log("no chat id; run `lights.py chatid` after /start")
        return False
    try:
        ok = tg("sendMessage", chat_id=chat, text=text, disable_web_page_preview="true").get("ok", False)
    except Exception as e:
        log(f"send failed: {type(e).__name__}")
        return False
    log(f"sent: {text[:80]!r}")
    return ok


def once(st: dict, key: str, text: str, dry: bool) -> None:
    if key in st["sent"]:
        return
    if send(st, text, dry):
        st["sent"][key] = now().isoformat(timespec="seconds")


def day_label(d: str, today: dt.date) -> str:
    dd = dt.date.fromisoformat(d)
    word = "сегодня" if dd == today else "завтра" if dd == today + dt.timedelta(days=1) else ""
    return f"{word} {dd:%d.%m}".strip()


def next_outage_line(sched: dict, t: dt.datetime) -> str:
    for a, b in abs_outages(sched, t.date()):
        if a <= t < b:
            return f"По графику сейчас света нет, вернут в {b:%H:%M}."
        if a > t:
            when = "" if a.date() == t.date() else " завтра"
            return f"Следующее отключение{when} {a:%H:%M}-{b:%H:%M}."
    return "Отключений по графику больше нет."


# ---------- the tick ----------

def tick(dry: bool) -> int:
    if not dry and not (_alert_token() and (os.getenv("LIGHTS_CHAT_ID") or _alert_chat())):
        sys.exit("[FATAL] Telegram token or alert chat id missing (EnvironmentFile not loaded)")
    st = load()
    st.setdefault("sched", {}); st.setdefault("sent", {}); st.setdefault("fail", 0)
    t = now()
    today = t.date()
    tomorrow = today + dt.timedelta(days=1)
    q = f"очередь {QUEUE}"

    res, errs = fetch_all()
    for name, e in errs.items():
        log(f"source {name} failed: {e}")
    if not any(res.values()):
        st["fail"] += 1
        if st["fail"] == FAIL_ALERT:
            send(st, f"❓ Ни один сайт с графиком не отвечает {st['fail'] * TICK_MIN} мин; проверю дальше.", dry)
        st["last_tick"] = t.isoformat(timespec="seconds")
        if not dry:
            save(st)
        return 1
    st["fail"] = 0
    cons, agree = consensus(res)
    for d, a in agree.items():
        if not a.startswith(a.split("/")[1]):
            log(f"sources disagree for {d}: {a}; " + ", ".join(f"{n}={describe(r[d])}" for n, r in res.items() if d in r))

    # nuc was down (power cut on our side, no UPS): say so once it is back
    last = st.get("last_tick")
    if last and t - dt.datetime.fromisoformat(last) > dt.timedelta(minutes=GAP_MIN):
        once(st, f"back:{last}", f"🔁 {HOST} снова в сети (не работал с {dt.datetime.fromisoformat(last):%H:%M}). "
             + next_outage_line({**st["sched"], **cons}, t), dry)

    first_run = not st["sched"]
    for d in (today.isoformat(), tomorrow.isoformat()):
        new, old = cons.get(d), st["sched"].get(d)
        if not new or new == old:
            continue
        h = hashlib.sha1(new.encode()).hexdigest()[:10]
        lbl = day_label(d, today)
        if old is None:
            text = f"📅 График на {lbl} ({q}):\n{column(new)}"
        else:
            text = f"⚠️ Изменился график на {lbl} ({q}):\n{column(new)}"
        if first_run and d == today.isoformat():
            text = f"✅ Слежу за светом ({q}, Чорноморськ). На {lbl}:\n{column(new)}"
        if send(st, text, dry):
            st["sent"][f"chg:{d}:{h}"] = t.isoformat(timespec="seconds")
            st["sched"][d] = new

    # reminders before an outage and before power returns
    for a, b in abs_outages(st["sched"], today):
        to_a, to_b = (a - t).total_seconds() / 60, (b - t).total_seconds() / 60
        if 0 < to_a <= WARN_OFF_MIN:
            once(st, f"off:{a:%F %H:%M}", f"🔌 Через {round(to_a)} мин отключение: {a:%H:%M}-{b:%H:%M}, вернут в {b:%H:%M}.", dry)
        if a <= t and 0 < to_b <= WARN_ON_MIN:
            once(st, f"on:{b:%F %H:%M}", f"💡 Свет должны вернуть в {b:%H:%M} (через {round(to_b)} мин). "
                 + next_outage_line(st["sched"], b + dt.timedelta(minutes=1)), dry)

    # morning summary (first tick after 09:00; catches up if 09:00 was dark)
    if t.time() >= MORNING and t.hour < 13:
        td, tm = st["sched"].get(today.isoformat()), st["sched"].get(tomorrow.isoformat())
        text = f"☀️ Сегодня {today:%d.%m} ({q}): " + (describe(td) if td else "график не опубликован") + "."
        if tm:
            text += f" Завтра: {describe(tm)}."
        once(st, f"morning:{today}", text, dry)

    # evening summary (owner 7 Nov 2026: 20:00): rest of tonight + tomorrow
    if t.time() >= EVENING:
        td, tm = st["sched"].get(today.isoformat()), st["sched"].get(tomorrow.isoformat())
        text = f"🌙 Вечер {today:%d.%m} ({q}). " + next_outage_line(st["sched"], t)
        text += f" Завтра {tomorrow:%d.%m}: " + (describe(tm) if tm else "график ещё не опубликован") + "."
        once(st, f"evening:{today}", text, dry)

    # keep state small
    keep = {(today + dt.timedelta(days=k)).isoformat() for k in (-1, 0, 1)}
    st["sched"] = {d: s for d, s in st["sched"].items() if d in keep}
    cutoff = (t - dt.timedelta(days=3)).isoformat()
    st["sent"] = {k: v for k, v in st["sent"].items() if v >= cutoff}
    st["last_tick"] = t.isoformat(timespec="seconds")
    if not dry:
        save(st)
    return 0


def show() -> int:
    res, errs = fetch_all()
    for n, e in errs.items():
        print(f"{n}: ERROR {e}")
    for n, r in res.items():
        for d, s in sorted(r.items()):
            print(f"{n:10} {d} {describe(s)}")
    cons, agree = consensus(res)
    for d, s in sorted(cons.items()):
        print(f"{'CONSENSUS':10} {d} {describe(s)} [agree {agree[d]}]")
    return 0 if cons else 1


def chatid() -> int:
    st = load()
    ups = tg("getUpdates").get("result", [])
    chats = [u["message"]["chat"] for u in ups if u.get("message", {}).get("chat", {}).get("type") == "private"]
    if not chats:
        print("no private message yet: send /start to the bot, then rerun")
        return 1
    st["chat_id"] = chats[-1]["id"]
    save(st)
    print("chat id stored")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    if cmd == "tick":
        return tick("--dry" in sys.argv)
    if cmd == "show":
        return show()
    if cmd == "chatid":
        return chatid()
    if cmd == "diag":  # why chatid sees nothing: webhook set, or updates consumed elsewhere; prints no ids
        w = tg("getWebhookInfo").get("result", {})
        print("webhook set:", bool(w.get("url")), "pending:", w.get("pending_update_count"))
        if _alert_chat():
            c = tg("getChat", chat_id=_alert_chat()).get("result", {})
            print("alert chat:", c.get("type"), c.get("title"))
        r = tg("getUpdates")
        print("ok:", r.get("ok"), "updates:", [(k, (v.get("chat") or {}).get("type")) for x in r.get("result", [])
                                              for k, v in x.items() if k != "update_id" and isinstance(v, dict)])
        return 0
    if cmd == "test":
        st = load()
        return 0 if send(st, f"🧪 Тест: бот света на связи ({now():%H:%M} Kyiv).", False) else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
