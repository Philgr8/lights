#!/usr/bin/env python3
"""Power-outage watcher: DTEK Odesa schedule for one queue -> Telegram private chat. Stdlib only, no LLM.

Sources (public pages, parsed by script): bezsvitla.com.ua, alerts.energy (oblenergo entry), svitlo.live.
Each source yields {date: 48 half-hour slots, '1' = no power}; the consensus is a slot-wise majority.

  lights.py tick [--dry]   one pass (systemd timer, every 30 min): changes, reminders, morning summary
  lights.py show           print each source and the consensus, send nothing
  lights.py test           send one test line

Env: TELEGRAM_BOT_TOKEN + TELEGRAM_OWNER_CHAT_ID (sink, private chat; never printed), LIGHTS_QUEUE (default 1.1), LIGHTS_STATE (state dir).
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
WARN_OFF_MIN = 45     # reminder before an outage (owner 8 Nov 2026: 45, ticks at :15/:45 so GitHub cron lag up to ~30 min still hits it)
WARN_ON_MIN = 45      # reminder before power returns (>= TICK_MIN or it is skipped; 45 with WARN_OFF_MIN)
TICK_MIN = 30         # lights.timer period (owner 7 Nov 2026: 30 min, was 5)
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


def _sink():
    """The one MP-1337 sender: tools/tg_widgets/sink.py, or a copy vendored beside this script (cloud runner)."""
    here = Path(__file__).resolve().parent
    for d in (here, here.parent / "tools" / "tg_widgets"):
        if (d / "sink.py").exists():
            sys.path.insert(0, str(d))
            import sink  # noqa: PLC0415
            return sink
    sys.exit("[FATAL] sink.py not found beside lights.py or in tools/tg_widgets")


def _bars(sched: dict, d: dt.date) -> list:
    return [[a / 2, b / 2, "r"] for a, b in intervals(sched[d.isoformat()])] if d.isoformat() in sched else []


def panel_data(sched: dict, t: dt.datetime, head: str = "") -> dict:
    """Pinned live panel (registry 'lights', widget kit keys): state chip, next outage, today/tomorrow timeline."""
    today, tm = t.date(), t.date() + dt.timedelta(days=1)
    nxt = next_outage_line(sched, t)
    off_now = any(a <= t < b for a, b in abs_outages(sched, today))
    soon = [(a - t).total_seconds() / 60 for a, b in abs_outages(sched, today) if t < a]
    if off_now:
        chip = ["r", "OFF"]
    elif soon and min(soon) <= WARN_OFF_MIN:
        chip = ["a", f"OFF IN {round(min(soon))}m"]
    else:
        chip = ["g", "ON"]
    rows = [[f"Today {today:%d.%m}", "", _bars(sched, today), round(t.hour + t.minute / 60, 2)]]
    if tm.isoformat() in sched:
        rows.append([f"Tomorrow {tm:%d.%m}", "", _bars(sched, tm)])
    return {"f": f"Lights, queue {QUEUE}", "c": chip, "a": f"{t:%H:%M}", "bg": 1, "h": head or nxt[:80], "t": rows,
            "x": [["Refresh chart", "lt:rf", "s"]]}


def change_data(chg: dict, t: dt.datetime) -> dict:
    """Schedule-change message: was / now / added for each changed day, timeline of the new schedule."""
    k, rows = [], []
    for d, (was, new) in sorted(chg.items()):
        lab = d[8:10] + "." + d[5:7]
        if was is not None and was != new:
            k += [[f"Was {lab}", column(was).replace("\n", ", ")], [f"Now {lab}", column(new).replace("\n", ", ")]]
            add = "".join("1" if x == "1" and w != "1" else "0" for x, w in zip(new, was))
            if "1" in add:
                k.append([f"Added {lab}", column(add).replace("\n", ", ")])
        elif was is None:
            k.append([f"New {lab}", column(new).replace("\n", ", ")])
        rows.append([f"{lab}", f"changed {t:%H:%M}", [[a / 2, b / 2, "r"] for a, b in intervals(new)]])
    return {"f": f"Lights, queue {QUEUE}", "c": ["r", "CHANGED"], "a": f"{t:%H:%M}", "h": "Schedule changed",
            "k": k[:6], "t": rows[:2], "x": [["Refresh chart", "lt:rf", "s"]]}


def send(st: dict, text: str, dry: bool, data: dict | None = None, sound: bool = False) -> bool:
    """Everything goes through the sink to the owner's private chat. sound=True: a NEW sounding message
    (schedule change). Otherwise a silent edit of the pinned panel; unchanged panel = nothing sent."""
    if dry:
        log(f"dry: {text[:80]!r}")
        return True
    if data is None:
        data = {"f": f"Lights, queue {QUEUE}", "c": ["a", "INFO"], "a": f"{now():%H:%M}", "h": text[:80]}
    try:
        r = _sink().send("lights", data, text.split("\n")[0][:200], state_dir=STATE_DIR, mode="" if sound else "panel")
    except Exception as e:
        log(f"send failed: {type(e).__name__}: {str(e)[:120]}")
        return False
    log(f"{r.get('action')}: {text[:80]!r}")
    return True


def once(st: dict, key: str, text: str, dry: bool) -> None:
    if key in st["sent"]:
        return
    if send(st, text, dry):
        st["sent"][key] = now().isoformat(timespec="seconds")


def day_label(d: str, today: dt.date) -> str:
    dd = dt.date.fromisoformat(d)
    word = "сегодня" if dd == today else "завтра" if dd == today + dt.timedelta(days=1) else ""
    return f"{word} {dd:%d.%m}".strip()


def span(a: dt.datetime, b: dt.datetime) -> str:
    """'23:00-24:00' like column(), not '23:00-00:00', when the outage ends at midnight."""
    end = "24:00" if b.time() == dt.time(0) and b.date() > a.date() else f"{b:%H:%M}"
    return f"{a:%H:%M}-{end}"


def next_outage_line(sched: dict, t: dt.datetime) -> str:
    for a, b in abs_outages(sched, t.date()):
        if a <= t < b:
            return f"По графику сейчас света нет, вернут в {b:%H:%M}."
        if a > t:
            when = "" if a.date() == t.date() else " завтра"
            return f"Следующее отключение{when} {span(a, b)}."
    return "Отключений по графику больше нет."


# ---------- the tick ----------

def tick(dry: bool) -> int:
    if not dry:
        if _sink().whoami():  # prints bot name and set/missing only
            sys.exit("[FATAL] bot token or owner chat env missing/wrong")
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
            send(st, f"❓ Ни один сайт с графиком не отвечает {st['fail'] * TICK_MIN} мин; проверю дальше.", dry,
                 {"f": f"Lights, queue {QUEUE}", "c": ["a", "NO DATA"], "a": f"{t:%H:%M}",
                  "h": f"No schedule source answers for {st['fail'] * TICK_MIN} min"})
        st["last_tick"] = t.isoformat(timespec="seconds")
        if not dry:
            save(st)
        return 1
    st["fail"] = 0
    cons, agree = consensus(res)
    for d, a in agree.items():
        if not a.startswith(a.split("/")[1]):
            log(f"sources disagree for {d}: {a}; " + ", ".join(f"{n}={describe(r[d])}" for n, r in res.items() if d in r))

    # Message layout (owner 8 Nov 2026, after two corrections):
    #  - at most ONE schedule message per tick: a change (⚠️, full schedule of both days) replaces the
    #    morning/evening summary of that tick; day blocks are '☀️ Сегодня 08.11:' + one interval per line,
    #    separated by a blank line; no totals, no 'next outage' line (it repeats the list);
    #  - reminders (🔌 / 💡) go as their own message;
    #  - no 'runner back online' message (owner: useless). Keys and the new schedule are committed only
    #    after a successful send, so a failed send retries whole next tick.
    first_run = not st["sched"]
    upd, changed = {}, {}   # date -> new slots; date -> (was_published_before, key)
    for d in (today.isoformat(), tomorrow.isoformat()):
        new, old = cons.get(d), st["sched"].get(d)
        if new and new != old:
            upd[d] = new
            changed[d] = (old is not None, f"chg:{d}:{hashlib.sha1(new.encode()).hexdigest()[:10]}")
    sched = {**st["sched"], **upd}

    td, tm = sched.get(today.isoformat()), sched.get(tomorrow.isoformat())

    def block(d: dt.date, word: str, slots, missing: str, suffix: str = "") -> str:
        mark = " (изменился)" if changed.get(d.isoformat(), (False,))[0] else ""
        return f"☀️ {word} {d:%d.%m}{suffix}{mark}:\n" + (column(slots) if slots else missing)

    def both_days(suffix: str = "") -> str:
        out = [block(today, "Сегодня", td, "график не опубликован", suffix)]
        if tm:
            out.append(block(tomorrow, "Завтра", tm, ""))
        return "\n\n".join(out)

    msg, keys = None, []
    morning_due = t.time() >= MORNING and t.hour < 13 and f"morning:{today}" not in st["sent"]
    evening_due = t.time() >= EVENING and f"evening:{today}" not in st["sent"]
    if changed:
        if first_run:
            top = f"✅ Слежу за светом ({q}, Чорноморськ)"
        elif any(was for was, _ in changed.values()):
            top = f"⚠️ Изменился график отключений ({q})"
        else:
            top = f"📅 Опубликован график ({q})"
        msg = top + "\n\n" + both_days()
        keys = [k for _, k in changed.values()]
        keys += [f"morning:{today}"] * morning_due + [f"evening:{today}"] * evening_due  # this replaces the summary
    elif morning_due:
        msg, keys = both_days(f" ({q})"), [f"morning:{today}"]
    elif evening_due:
        msg = f"🌙 Вечер {today:%d.%m} ({q}). " + next_outage_line(sched, t) + "\n\n" \
            + block(tomorrow, "Завтра", tm, "график ещё не опубликован")
        keys = [f"evening:{today}"]
    chg = {d: (st["sched"].get(d), upd[d]) for d in upd}
    sounding = bool(changed) and not first_run and any(was for was, _ in changed.values())
    if msg is None:
        ok = True
    elif sounding:   # a schedule change: a NEW sounding message, then the pinned panel follows silently
        ok = send(st, msg, dry, change_data(chg, t), sound=True)
        if ok:
            send(st, msg, dry, panel_data(sched, t, "Schedule changed " + f"{t:%H:%M}"))
    else:
        ok = send(st, msg, dry, panel_data(sched, t, msg.split("\n")[0][:80]))
    if ok:
        for k in keys:
            st["sent"][k] = t.isoformat(timespec="seconds")
        st["sched"].update(upd)

    # reminders before an outage and before power returns: their own message
    rem = []
    for a, b in abs_outages(sched, today):
        to_a, to_b = (a - t).total_seconds() / 60, (b - t).total_seconds() / 60
        if 0 < to_a <= WARN_OFF_MIN and f"off:{a:%F %H:%M}" not in st["sent"]:
            rem.append((f"off:{a:%F %H:%M}", f"🔌 Через {round(to_a)} мин отключение: {span(a, b)}, вернут в {b:%H:%M}."))
        if a <= t and 0 < to_b <= WARN_ON_MIN and f"on:{b:%F %H:%M}" not in st["sent"]:
            rem.append((f"on:{b:%F %H:%M}", f"💡 Свет должны вернуть в {b:%H:%M} (через {round(to_b)} мин). "
                        + next_outage_line(sched, b + dt.timedelta(minutes=1))))
    if rem and send(st, "\n".join(x for _, x in rem), dry, panel_data(sched, t, rem[0][1][:80])):
        for k, _ in rem:
            st["sent"][k] = t.isoformat(timespec="seconds")

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


def refresh() -> int:
    """Button press: send the current consensus schedule to the owner chat, no dedupe."""
    res, _ = fetch_all()
    cons, _ = consensus(res)
    if not cons:
        send(load(), "Could not fetch the schedule, try later.", False)
        return 1
    today = now().date().isoformat()
    lines = [f"{d}\n{column(s)}" for d, s in sorted(cons.items()) if d >= today]
    return 0 if send(load(), "\n\n".join(lines) or "No data for today.", False) else 1


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    if cmd == "tick":
        return tick("--dry" in sys.argv)
    if cmd == "show":
        return show()
    if cmd == "refresh":
        return refresh()
    if cmd == "chatid":
        sys.exit("[FATAL] chatid retired (MP-1337): the sink sends to TELEGRAM_OWNER_CHAT_ID")
    if cmd == "test":
        st = load()
        return 0 if send(st, f"Test: lights bot is up ({now():%H:%M} Kyiv).", False) else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
