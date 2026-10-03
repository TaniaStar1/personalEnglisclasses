#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Телеграм-бот для учёта частных уроков.
Один файл, только стандартная библиотека Python 3.8+, база — SQLite.

Переменные окружения:
  TG_BOT_TOKEN    — токен бота от @BotFather (обязательно)
  TG_ADMIN_ID     — ваш числовой Telegram ID (обязательно)
  TG_BOT_DB       — полный путь к файлу базы (обязательно)
  TG_BACKUP_HOUR  — час ежедневной отправки копии базы, 0–23 (по умолчанию 21)
  TG_TZ_OFFSET    — ваш часовой пояс, смещение от UTC в часах (по умолчанию 3 = Москва)
"""

import html
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("tutorbot")

CURRENCY = "₽"
WD_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WD_LOWER = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
STATUS_ICON = {"scheduled": "🕒", "done": "✅", "cancelled": "❌"}
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

TOKEN = ""
ADMIN_ID = 0
DB_PATH = ""
BACKUP_HOUR = 21
TZ = timezone(timedelta(hours=3))
BOT_USERNAME = ""

conn = None
STATE = {}            # chat_id -> {"s": имя шага, ...}; незавершённые диалоги
NEXT_BACKUP_TRY = 0.0

# ───────────────────────────── настройки ─────────────────────────────


def load_config():
    global TOKEN, ADMIN_ID, DB_PATH, BACKUP_HOUR, TZ
    errors = []
    TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
    if not TOKEN:
        errors.append("TG_BOT_TOKEN не задан — укажите токен бота от @BotFather.")
    try:
        ADMIN_ID = int(os.environ.get("TG_ADMIN_ID", "").strip())
    except ValueError:
        errors.append("TG_ADMIN_ID не задан или не число — укажите ваш числовой Telegram ID.")
    DB_PATH = os.environ.get("TG_BOT_DB", "").strip()
    if not DB_PATH:
        errors.append("TG_BOT_DB не задан — укажите полный путь к файлу базы "
                      "(в папке, которая сохраняется между перезапусками).")
    else:
        DB_PATH = os.path.abspath(DB_PATH)
        folder = os.path.dirname(DB_PATH)
        if not os.path.isdir(folder):
            errors.append(f"Папка для базы не существует: {folder}")
    try:
        BACKUP_HOUR = int(os.environ.get("TG_BACKUP_HOUR", "21").strip())
        if not 0 <= BACKUP_HOUR <= 23:
            raise ValueError
    except ValueError:
        errors.append("TG_BACKUP_HOUR должен быть числом от 0 до 23.")
    try:
        off = float(os.environ.get("TG_TZ_OFFSET", "3").strip().replace(",", "."))
        TZ = timezone(timedelta(hours=off))
    except ValueError:
        errors.append("TG_TZ_OFFSET должен быть числом, например 3 для Москвы.")
    if errors:
        for e in errors:
            print("ОШИБКА НАСТРОЙКИ:", e, file=sys.stderr)
        sys.exit(1)


def now():
    return datetime.now(TZ)


def today():
    return now().date()

# ───────────────────────────── база данных ─────────────────────────────


SCHEMA = """
CREATE TABLE IF NOT EXISTS students(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    rate INTEGER NOT NULL DEFAULT 0,
    duration INTEGER NOT NULL DEFAULT 60,
    invite_code TEXT,
    tg_chat_id INTEGER,
    archived INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slots(
    id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL,
    weekday INTEGER NOT NULL,
    time TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lessons(
    id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL,
    slot_id INTEGER,
    orig_date TEXT,
    date TEXT NOT NULL,
    time TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'scheduled',
    charged INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    moved_from TEXT NOT NULL DEFAULT '',
    updated TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS lessons_slot_occurrence
    ON lessons(slot_id, orig_date) WHERE slot_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS lessons_date ON lessons(date);
CREATE TABLE IF NOT EXISTS payments(
    id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL,
    lessons INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS homework(
    id INTEGER PRIMARY KEY,
    student_id INTEGER NOT NULL,
    lesson_id INTEGER,
    text TEXT NOT NULL,
    created TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS homework_files(
    id INTEGER PRIMARY KEY,
    homework_id INTEGER NOT NULL,
    from_chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""
REQUIRED_TABLES = {"students", "slots", "lessons", "payments", "homework"}


def open_db():
    global conn
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(SCHEMA)
    conn.commit()


def q(sql, *a):
    return conn.execute(sql, a).fetchall()


def q1(sql, *a):
    return conn.execute(sql, a).fetchone()


def x(sql, *a):
    cur = conn.execute(sql, a)
    conn.commit()
    return cur.lastrowid


def get_setting(key):
    r = q1("SELECT value FROM settings WHERE key=?", key)
    return r["value"] if r else None


def set_setting(key, value):
    x("INSERT INTO settings(key,value) VALUES(?,?) "
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, value)

# ───────────────────────────── Telegram API ─────────────────────────────


def call(method, params=None, timeout=40):
    """Возвращает (ok, result | описание ошибки)."""
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = json.dumps(params or {}).encode("utf-8")
    for attempt in range(3):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                res = json.loads(e.read().decode("utf-8"))
            except Exception:
                res = {"ok": False, "description": str(e)}
        except Exception as e:
            log.warning("Сеть (%s): %s", method, e)
            time.sleep(2 + attempt * 3)
            continue
        if res.get("ok"):
            return True, res.get("result")
        retry = (res.get("parameters") or {}).get("retry_after")
        if retry:
            time.sleep(min(int(retry), 60))
            continue
        desc = res.get("description", "")
        if "not modified" not in desc:
            log.warning("API %s: %s", method, desc)
        return False, desc
    return False, "network error"


def api(method, params=None, timeout=40):
    ok, res = call(method, params, timeout)
    return res if ok else None


def btn(text, data):
    return {"text": text, "callback_data": data}


def clip(text, limit=4000):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def send(chat_id, text, kb=None):
    p = {"chat_id": chat_id, "text": clip(text), "parse_mode": "HTML",
         "disable_web_page_preview": True}
    if kb:
        p["reply_markup"] = {"inline_keyboard": kb}
    return api("sendMessage", p)


def show(chat_id, text, kb=None, mid=None):
    """Обновляет сообщение с кнопками, если можно, иначе шлёт новое."""
    if mid:
        ok, res = call("editMessageText", {
            "chat_id": chat_id, "message_id": mid, "text": clip(text), "parse_mode": "HTML",
            "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": kb or []}})
        if ok or "not modified" in str(res):
            return
    send(chat_id, text, kb)


def copy_message(to_chat, from_chat, message_id):
    return api("copyMessage", {"chat_id": to_chat, "from_chat_id": from_chat,
                               "message_id": message_id}) is not None


def send_document(chat_id, path, filename, caption=""):
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        content = f.read()
    body = b""
    for k, v in (("chat_id", str(chat_id)), ("caption", caption)):
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                 f"{v}\r\n").encode("utf-8")
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
             f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
             ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    url = f"https://api.telegram.org/bot{TOKEN}/sendDocument"
    for attempt in range(3):
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                if json.loads(r.read().decode("utf-8")).get("ok"):
                    return True
        except Exception as e:
            log.warning("Не удалось отправить файл: %s", e)
        time.sleep(3 + attempt * 5)
    return False


def download_file(file_id, dest):
    info = api("getFile", {"file_id": file_id})
    if not info or "file_path" not in info:
        return False
    url = f"https://api.telegram.org/file/bot{TOKEN}/{info['file_path']}"
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        return True
    except Exception as e:
        log.warning("Не удалось скачать файл: %s", e)
        return False

# ───────────────────────────── форматирование и разбор ─────────────────────────────


def esc(s):
    return html.escape(str(s or ""), quote=False)


def plural(n, one, few, many):
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def lessons_word(n):
    return f"{n} {plural(n, 'занятие', 'занятия', 'занятий')}"


def money(n):
    return f"{n:,}".replace(",", " ") + " " + CURRENCY


def fmt_date(iso):
    d = date.fromisoformat(iso)
    return f"{WD_SHORT[d.weekday()]} {d:%d.%m}"


def fmt_when(l):
    return f"{fmt_date(l['date'])}, {l['time']}" if l.get("time") else fmt_date(l["date"])


def parse_time(s):
    m = re.fullmatch(r"(\d{1,2})(?:[:.\-](\d{2}))?", s.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def parse_date(s, future=False):
    s = s.strip().lower()
    t = today()
    words = {"сегодня": 0, "завтра": 1, "послезавтра": 2, "вчера": -1, "позавчера": -2}
    if s in words:
        return t + timedelta(days=words[s])
    m = re.fullmatch(r"(\d{1,2})[./\-](\d{1,2})(?:[./\-](\d{2,4}))?", s)
    if not m:
        return None
    d, mo = int(m.group(1)), int(m.group(2))
    y = int(m.group(3)) if m.group(3) else t.year
    if y < 100:
        y += 2000
    try:
        res = date(y, mo, d)
    except ValueError:
        return None
    if not m.group(3):
        try:
            if future and res < t - timedelta(days=60):
                res = res.replace(year=y + 1)
            elif not future and res > t + timedelta(days=60):
                res = res.replace(year=y - 1)
        except ValueError:
            pass
    return res


def parse_date_time(text, future=True):
    """'03.10 18:00' → (date, '18:00'); 'сегодня 18' тоже подходит.
    Если указано только одно из двух, второе будет None. Ошибка → (False, False)."""
    parts = text.replace(",", " ").split()
    if len(parts) == 1:
        p = parts[0]
        if ":" in p:
            t = parse_time(p)
            return (None, t) if t else (False, False)
        d = parse_date(p, future)
        return (d, None) if d else (False, False)
    if len(parts) == 2:
        d, t = parse_date(parts[0], future), parse_time(parts[1])
        if d and t:
            return d, t
    return False, False

# ───────────────────────────── ученики, баланс ─────────────────────────────


def student(sid):
    return q1("SELECT * FROM students WHERE id=?", sid)


def balance(sid):
    paid = q1("SELECT COALESCE(SUM(lessons),0) FROM payments WHERE student_id=?", sid)[0]
    used = q1("SELECT COUNT(*) FROM lessons WHERE student_id=? AND charged=1", sid)[0]
    return paid, used, paid - used


def balance_line(st, detailed=False):
    paid, used, rem = balance(st["id"])
    if rem > 0:
        s = f"Остаток: <b>{lessons_word(rem)}</b>"
    elif rem == 0:
        s = "Остаток: <b>0</b> — оплаченные занятия закончились"
    else:
        s = f"⚠️ Долг: <b>{lessons_word(-rem)}</b>"
        if st["rate"]:
            s += f" (≈ {money(-rem * st['rate'])})"
    if detailed:
        s += f"\n<i>оплачено {paid}, списано {used}</i>"
    return s


def slots_text(sid):
    rows = q("SELECT * FROM slots WHERE student_id=? ORDER BY weekday, time", sid)
    return ", ".join(f"{WD_LOWER[r['weekday']]} {r['time']}" for r in rows)


def new_code():
    while True:
        c = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not q1("SELECT 1 FROM students WHERE invite_code=?", c):
            return c


def linked_student(uid):
    return q1("SELECT * FROM students WHERE tg_chat_id=? AND archived=0 ORDER BY id LIMIT 1", uid)

# ───────────────────────────── расписание ─────────────────────────────
# Постоянные слоты разворачиваются в занятия «на лету». Запись в lessons появляется,
# когда с конкретным занятием что-то делают (провела, перенос, отмена) или
# когда занятие разовое. Ключ занятия: "L<id>" — запись в базе,
# "S<slot>_<ГГГГММДД>" — занятие по постоянному слоту, которого ещё нет в базе.


def get_lessons(d1, d2, sid=None):
    a1, a2 = d1.isoformat(), d2.isoformat()
    res = []
    sql = ("SELECT l.*, s.name, s.duration FROM lessons l JOIN students s ON s.id=l.student_id "
           "WHERE s.archived=0 AND l.date BETWEEN ? AND ?")
    args = [a1, a2]
    if sid:
        sql += " AND l.student_id=?"
        args.append(sid)
    for r in conn.execute(sql, args):
        d = dict(r)
        d["key"] = f"L{r['id']}"
        d["virtual"] = False
        res.append(d)
    overridden = {(r[0], r[1]) for r in conn.execute(
        "SELECT slot_id, orig_date FROM lessons WHERE slot_id IS NOT NULL "
        "AND orig_date BETWEEN ? AND ?", (a1, a2))}
    sql = ("SELECT sl.*, s.name, s.duration FROM slots sl JOIN students s ON s.id=sl.student_id "
           "WHERE s.archived=0")
    args = []
    if sid:
        sql += " AND sl.student_id=?"
        args.append(sid)
    slots = conn.execute(sql, args).fetchall()
    d = d1
    while d <= d2:
        ds = d.isoformat()
        for sl in slots:
            if sl["weekday"] == d.weekday() and ds >= sl["created"] and (sl["id"], ds) not in overridden:
                res.append({"key": f"S{sl['id']}_{d:%Y%m%d}", "virtual": True,
                            "student_id": sl["student_id"], "slot_id": sl["id"], "orig_date": ds,
                            "name": sl["name"], "duration": sl["duration"], "date": ds,
                            "time": sl["time"], "status": "scheduled", "charged": 0,
                            "note": "", "moved_from": ""})
        d += timedelta(days=1)
    res.sort(key=lambda l: (l["date"], l["time"] or "99", l["name"]))
    return res


def lesson_info(key):
    if key.startswith("L") and key[1:].isdigit():
        r = q1("SELECT l.*, s.name, s.duration FROM lessons l JOIN students s "
               "ON s.id=l.student_id WHERE l.id=?", int(key[1:]))
        if not r:
            return None
        d = dict(r)
        d["key"] = key
        d["virtual"] = False
        return d
    m = re.fullmatch(r"S(\d+)_(\d{4})(\d{2})(\d{2})", key)
    if not m:
        return None
    slot_id = int(m.group(1))
    ds = f"{m.group(2)}-{m.group(3)}-{m.group(4)}"
    r = q1("SELECT id FROM lessons WHERE slot_id=? AND orig_date=?", slot_id, ds)
    if r:
        return lesson_info(f"L{r['id']}")
    sl = q1("SELECT sl.*, s.name, s.duration FROM slots sl JOIN students s "
            "ON s.id=sl.student_id WHERE sl.id=?", slot_id)
    if not sl:
        return None
    return {"key": key, "virtual": True, "student_id": sl["student_id"], "slot_id": slot_id,
            "orig_date": ds, "name": sl["name"], "duration": sl["duration"], "date": ds,
            "time": sl["time"], "status": "scheduled", "charged": 0, "note": "", "moved_from": ""}


def materialize(key):
    """Гарантирует, что занятие есть в базе; возвращает id записи."""
    info = lesson_info(key)
    if not info:
        return None
    if not info["virtual"]:
        return info["id"]
    return x("INSERT INTO lessons(student_id, slot_id, orig_date, date, time, status, updated) "
             "VALUES(?,?,?,?,?,'scheduled',?)", info["student_id"], info["slot_id"],
             info["orig_date"], info["date"], info["time"], now().isoformat())


def next_lesson(sid):
    n = now()
    t, hm = n.date(), n.strftime("%H:%M")
    for l in get_lessons(t, t + timedelta(days=60), sid):
        if l["status"] != "scheduled":
            continue
        if l["date"] == t.isoformat() and l["time"] and l["time"] < hm:
            continue
        return l
    return None


def lesson_line(l, with_date=False, with_name=True):
    s = f"{STATUS_ICON[l['status']]} "
    if with_date:
        s += fmt_date(l["date"]) + " "
    s += l["time"] or "—"
    if with_name:
        s += " " + esc(l["name"])
    if l["status"] == "cancelled":
        s += " — отменено"
    elif l["moved_from"]:
        s += " (перенос)"
    return s

# ───────────────────────────── экраны преподавателя ─────────────────────────────


def main_menu(cid, mid=None):
    n = len([l for l in get_lessons(today(), today()) if l["status"] == "scheduled"])
    text = (f"📒 <b>Учёт занятий</b>\nСегодня {fmt_date(today().isoformat())}: "
            f"{'занятий нет' if n == 0 else 'запланировано ' + lessons_word(n)}.")
    kb = [[btn("👥 Ученики", "m:students")],
          [btn("📅 Сегодня", "m:today"), btn("🗓 Неделя", "m:week")],
          [btn("💾 Копия базы", "m:backup"), btn("📥 Восстановить", "m:restore")]]
    show(cid, text, kb, mid)


def students_list(cid, mid=None, archived=False):
    rows = q("SELECT * FROM students WHERE archived=? ORDER BY name COLLATE NOCASE", int(archived))
    kb = []
    for st in rows:
        _, _, rem = balance(st["id"])
        tail = f" · долг {-rem}" if rem < 0 else f" · {rem}"
        kb.append([btn(st["name"] + tail, f"s:{st['id']}")])
    if archived:
        text = "📦 <b>Архив</b>" + ("" if rows else "\nПусто.")
        kb.append([btn("⬅️ Ученики", "m:students")])
    else:
        text = "👥 <b>Ученики</b>" + ("\nПока никого — добавьте первого." if not rows else
                                     "\n<i>цифра — остаток оплаченных занятий</i>")
        kb.append([btn("➕ Добавить ученика", "m:add")])
        n_arch = q1("SELECT COUNT(*) FROM students WHERE archived=1")[0]
        if n_arch:
            kb.append([btn(f"📦 Архив ({n_arch})", "m:archive")])
        kb.append([btn("⬅️ Меню", "m:main")])
    show(cid, text, kb, mid)


def teacher_card(cid, sid, mid=None):
    st = student(sid)
    nl = next_lesson(sid)
    lines = [f"👤 <b>{esc(st['name'])}</b>" + (" (в архиве)" if st["archived"] else ""),
             f"Ставка: {money(st['rate'])} · {st['duration']} мин",
             balance_line(st, detailed=True),
             f"Ближайшее: {fmt_when(nl) if nl else '—'}",
             f"Постоянно: {slots_text(sid) or '—'}",
             "Бот: " + ("подключён ✅" if st["tg_chat_id"] else "не подключён")]
    if st["archived"]:
        kb = [[btn("♻️ Вернуть из архива", f"s:{sid}:unarch")], [btn("⬅️ Архив", "m:archive")]]
    else:
        kb = [[btn("✅ Провела", f"s:{sid}:done"), btn("💳 Оплата", f"s:{sid}:pay")],
              [btn("🗓 Постоянные слоты", f"s:{sid}:slots"), btn("➕ Разовое", f"s:{sid}:oneoff")],
              [btn("📋 Ближайшие занятия", f"s:{sid}:lessons"), btn("📚 Домашка", f"s:{sid}:hw")],
              [btn("✏️ Имя", f"s:{sid}:name"), btn("💰 Ставка", f"s:{sid}:rate"),
               btn("⏱ Длительность", f"s:{sid}:dur")],
              [btn("🔑 Код приглашения", f"s:{sid}:code"), btn("📦 В архив", f"s:{sid}:arch")],
              [btn("⬅️ Ученики", "m:students")]]
    show(cid, "\n".join(lines), kb, mid)


def schedule_view(cid, days, mid=None):
    t = today()
    ls = get_lessons(t, t + timedelta(days=days - 1))
    if days == 1:
        title = f"📅 <b>Сегодня, {fmt_date(t.isoformat())}</b>"
    else:
        title = f"🗓 <b>Неделя: {t:%d.%m} – {t + timedelta(days=days - 1):%d.%m}</b>"
    lines, kb, cur = [title], [], None
    if not ls:
        lines.append("\nЗанятий нет.")
    for l in ls:
        if days > 1 and l["date"] != cur:
            cur = l["date"]
            lines.append(f"\n<b>{fmt_date(cur)}</b>")
        lines.append(lesson_line(l))
        if len(kb) < 90:
            label = f"{STATUS_ICON[l['status']]} " + ("" if days == 1 else fmt_date(l["date"]) + " ") \
                    + f"{l['time'] or '—'} {l['name']}"
            kb.append([btn(label[:60], f"l:{l['key']}")])
    if ls:
        lines.append("\n<i>Нажмите на занятие, чтобы отметить, перенести или отменить.</i>")
    kb.append([btn("🔄 Обновить", "m:today" if days == 1 else "m:week"), btn("⬅️ Меню", "m:main")])
    show(cid, "\n".join(lines), kb, mid)


def student_lessons_view(cid, sid, mid=None):
    st = student(sid)
    t = today()
    ls = get_lessons(t, t + timedelta(days=13), sid)
    lines = [f"📋 <b>{esc(st['name'])}: занятия на 2 недели</b>"]
    kb = []
    if not ls:
        lines.append("Занятий нет. Добавьте постоянный слот или разовое занятие.")
    for l in ls:
        lines.append(lesson_line(l, with_date=True, with_name=False))
        kb.append([btn(f"{STATUS_ICON[l['status']]} {fmt_when(l)}", f"l:{l['key']}")])
    kb.append([btn("⬅️ Карточка", f"s:{sid}")])
    show(cid, "\n".join(lines), kb, mid)


def slots_view(cid, sid, mid=None):
    st = student(sid)
    rows = q("SELECT * FROM slots WHERE student_id=? ORDER BY weekday, time", sid)
    text = f"🗓 <b>Постоянные слоты: {esc(st['name'])}</b>\n"
    text += "\n".join(f"• {WD_SHORT[r['weekday']]} {r['time']}" for r in rows) if rows else "Пока нет."
    kb = [[btn(f"🗑 Удалить {WD_SHORT[r['weekday']]} {r['time']}", f"slotdel:{r['id']}")] for r in rows]
    kb.append([btn("➕ Добавить слот", f"slotadd:{sid}")])
    kb.append([btn("⬅️ Карточка", f"s:{sid}")])
    show(cid, text, kb, mid)


def lesson_view(cid, key, mid=None):
    info = lesson_info(key)
    if not info:
        show(cid, "Занятие не найдено.", [[btn("⬅️ Меню", "m:main")]], mid)
        return
    lines = [f"<b>{esc(info['name'])}</b>", f"{fmt_when(info)} · {info['duration']} мин"]
    if info["status"] == "scheduled":
        lines.append("🕒 запланировано")
    elif info["status"] == "done":
        lines.append("✅ проведено")
    else:
        lines.append("❌ отменено " + ("со списанием" if info["charged"] else "без списания"))
        if info["note"]:
            lines.append("Причина: " + esc(info["note"]))
    if info["moved_from"]:
        lines.append("🔁 перенесено, было: " + esc(info["moved_from"]))
    k = info["key"]
    if info["status"] == "scheduled":
        kb = [[btn("✅ Провела", f"ld:{k}")],
              [btn("🔁 Перенести", f"lm:{k}"), btn("❌ Отменить", f"lc:{k}")]]
    else:
        kb = [[btn("↩️ Вернуть в расписание", f"lu:{k}")]]
    kb.append([btn("👤 Карточка", f"s:{info['student_id']}"), btn("📅 Сегодня", "m:today")])
    show(cid, "\n".join(lines), kb, mid)


def code_view(cid, sid, mid=None):
    st = student(sid)
    code = st["invite_code"]
    if not code:
        code = new_code()
        x("UPDATE students SET invite_code=? WHERE id=?", code, sid)
    lines = [f"🔑 <b>Код приглашения: {esc(st['name'])}</b>", "", f"Код: <code>{code}</code>"]
    if BOT_USERNAME:
        lines.append(f"Ссылка: https://t.me/{BOT_USERNAME}?start={code}")
    lines += ["", "Ученик открывает ссылку или отправляет боту этот код. Код одноразовый."]
    if st["tg_chat_id"]:
        lines.append("\nУченик уже подключён. Если он сменил аккаунт — дайте ему новый код, "
                     "подключение перейдёт на новый аккаунт.")
    kb = [[btn("🔄 Новый код", f"s:{sid}:newcode")], [btn("⬅️ Карточка", f"s:{sid}")]]
    show(cid, "\n".join(lines), kb, mid)


def latest_hw(sid):
    return q1("SELECT * FROM homework WHERE student_id=? ORDER BY id DESC LIMIT 1", sid)


def hw_files(hw_id):
    return q("SELECT * FROM homework_files WHERE homework_id=? ORDER BY id", hw_id)


HW_SHOWN = 2          # сколько последних домашних заданий показывать


def recent_hws(sid, n=HW_SHOWN):
    return q("SELECT * FROM homework WHERE student_id=? ORDER BY id DESC LIMIT ?", sid, n)


def hw_title(i, hw):
    label = "Текущее задание" if i == 0 else "Предыдущее задание"
    return f"📚 <b>{label}</b> ({fmt_date(hw['created'][:10])})"


def hw_view(cid, sid, mid=None):
    st = student(sid)
    hws = recent_hws(sid)
    kb = [[btn("✍️ Новая домашка", f"hw:{sid}:0")]]
    if hws:
        parts = [f"📚 <b>Домашка: {esc(st['name'])}</b>"]
        for i, hw in enumerate(hws):
            files = hw_files(hw["id"])
            block = f"{hw_title(i, hw)}\n{esc(clip(hw['text'], 1700))}"
            if files:
                block += f"\n📎 Вложений: {len(files)}"
                kb.append([btn(f"📎 Вложения от {fmt_date(hw['created'][:10])}", f"hwfi:{hw['id']}")])
            block += "\n" + ("✅ Доставлено ученику" if hw["delivered"] else "Ученику не доставлено")
            parts.append(block)
        text = "\n\n".join(parts)
        if st["tg_chat_id"]:
            kb.append([btn("📤 Отправить текущее ещё раз", f"hwre:{sid}")])
    else:
        text = f"📚 <b>Домашка: {esc(st['name'])}</b>\nПока не задавалась."
    kb.append([btn("⬅️ Карточка", f"s:{sid}")])
    show(cid, text, kb, mid)

# ───────────────────────────── действия ─────────────────────────────


def reminder_text(st):
    t = (f"Здравствуйте, {st['name']}! Напоминаю: у вас осталось последнее оплаченное занятие. "
         f"Чтобы мы продолжили без перерыва, пожалуйста, оплатите следующий пакет занятий.")
    if st["rate"]:
        t += f" Стоимость одного занятия — {money(st['rate'])}."
    return t + " Спасибо!"


def check_reminder(cid, sid):
    st = student(sid)
    if balance(sid)[2] != 1:
        return
    kb = [[btn("📤 Отправить ученику", f"rem:{sid}")]] if st["tg_chat_id"] else None
    send(cid, f"🔔 У <b>{esc(st['name'])}</b> осталось последнее оплаченное занятие.\n"
              + ("Ниже — готовый текст. Можно переслать его или отправить кнопкой."
                 if kb else "Ниже — готовый текст, перешлите его ученику."), kb)
    send(cid, esc(reminder_text(st)))


def mark_done(lid):
    x("UPDATE lessons SET status='done', charged=1, note='', updated=? WHERE id=?",
      now().isoformat(), lid)


def done_followup(cid, lid, mid=None):
    info = lesson_info(f"L{lid}")
    sid = info["student_id"]
    st = student(sid)
    show(cid, f"✅ Отмечено: <b>{esc(st['name'])}</b>, {fmt_when(info)}\n{balance_line(st)}",
         [[btn("↩️ Отменить отметку", f"lu:L{lid}"), btn("👤 Карточка", f"s:{sid}")]], mid)
    check_reminder(cid, sid)
    send(cid, f"📚 Написать домашнее задание для {esc(st['name'])}?",
         [[btn("✍️ Написать", f"hw:{sid}:{lid}"), btn("Не сейчас", f"s:{sid}")]])


def done_on_date(cid, sid, d, mid=None):
    scheduled = [l for l in get_lessons(d, d, sid) if l["status"] == "scheduled"]
    if scheduled:
        lid = materialize(scheduled[0]["key"])
    else:
        lid = x("INSERT INTO lessons(student_id, date, time, status, updated) "
                "VALUES(?,?,'','scheduled',?)", sid, d.isoformat(), now().isoformat())
    mark_done(lid)
    done_followup(cid, lid, mid)


def finish_cancel(cid, lid, charged, reason):
    x("UPDATE lessons SET status='cancelled', charged=?, note=?, updated=? WHERE id=?",
      int(charged), reason[:300], now().isoformat(), lid)
    info = lesson_info(f"L{lid}")
    st = student(info["student_id"])
    send(cid, f"❌ Отменено {'со списанием' if charged else 'без списания'}: "
              f"<b>{esc(st['name'])}</b>, {fmt_when(info)}\n{balance_line(st)}",
         [[btn("↩️ Вернуть в расписание", f"lu:L{lid}"), btn("👤 Карточка", f"s:{st['id']}")]])
    if charged:
        check_reminder(cid, st["id"])


def deliver_hw(hw_id):
    """Отправляет домашку ученику. Возвращает текст для преподавателя."""
    hw = q1("SELECT * FROM homework WHERE id=?", hw_id)
    st = student(hw["student_id"])
    if not st["tg_chat_id"]:
        return "Ученик ещё не подключён к боту — он увидит задание в своей карточке после подключения."
    ok = send(st["tg_chat_id"], f"📚 <b>Домашнее задание</b> ({fmt_date(hw['created'][:10])})\n\n"
                                f"{esc(hw['text'])}")
    if not ok:
        return "⚠️ Не удалось доставить: возможно, ученик остановил бота. Задание сохранено в его карточке."
    failed = sum(0 if copy_message(st["tg_chat_id"], f["from_chat_id"], f["message_id"]) else 1
                 for f in hw_files(hw_id))
    x("UPDATE homework SET delivered=1 WHERE id=?", hw_id)
    return "✅ Отправлено ученику." + (f" Не удалось переслать вложений: {failed}." if failed else "")


def backup_now(cid, caption_prefix="🗄 Копия базы"):
    ts = now().strftime("%Y-%m-%d_%H-%M")
    tmp = os.path.join(os.path.dirname(DB_PATH), f".backup-{uuid.uuid4().hex}.tmp")
    try:
        dst = sqlite3.connect(tmp)
        conn.backup(dst)
        dst.close()
        n_st = q1("SELECT COUNT(*) FROM students WHERE archived=0")[0]
        n_l = q1("SELECT COUNT(*) FROM lessons")[0]
        caption = f"{caption_prefix} от {ts.replace('_', ' ')}. Учеников: {n_st}, записей о занятиях: {n_l}."
        return send_document(cid, tmp, f"lessons_backup_{ts}.db", caption)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def maybe_auto_backup():
    global NEXT_BACKUP_TRY
    n = now()
    if n.hour < BACKUP_HOUR or time.time() < NEXT_BACKUP_TRY:
        return
    day = n.date().isoformat()
    if get_setting("last_auto_backup") == day:
        return
    if backup_now(ADMIN_ID, "🗄 Ежедневная копия базы"):
        set_setting("last_auto_backup", day)
    else:
        NEXT_BACKUP_TRY = time.time() + 600


def validate_db(path):
    """Возвращает (описание содержимого, None) или (None, ошибка)."""
    with open(path, "rb") as f:
        if f.read(16) != b"SQLite format 3\x00":
            return None, "Это не файл базы SQLite."
    c = None
    try:
        c = sqlite3.connect(path)
        r = c.execute("PRAGMA integrity_check").fetchone()[0]
        if r != "ok":
            return None, f"Файл базы повреждён ({r})."
        names = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = REQUIRED_TABLES - names
        if missing:
            return None, "Это база не от этого бота (нет таблиц: " + ", ".join(sorted(missing)) + ")."
        n_st = c.execute("SELECT COUNT(*) FROM students").fetchone()[0]
        n_l = c.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        n_p = c.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        return f"учеников: {n_st}, записей о занятиях: {n_l}, оплат: {n_p}", None
    except sqlite3.DatabaseError as e:
        return None, f"Файл не читается как база: {e}"
    finally:
        if c:
            c.close()


def do_restore(tmp):
    stamp = now().strftime("%Y%m%d-%H%M%S")
    old = f"{DB_PATH}.before-restore-{stamp}"
    conn.close()
    try:
        if os.path.exists(DB_PATH):
            os.replace(DB_PATH, old)
        for suf in ("-journal", "-wal", "-shm"):
            if os.path.exists(DB_PATH + suf):
                os.replace(DB_PATH + suf, old + suf)
        os.replace(tmp, DB_PATH)
    except Exception:
        if not os.path.exists(DB_PATH) and os.path.exists(old):
            os.replace(old, DB_PATH)
        open_db()
        raise
    open_db()
    return old


def start_restore(cid):
    STATE[cid] = {"s": "restore"}
    send(cid, "📥 <b>Восстановление базы</b>\nПришлите файл копии (.db) документом. "
              "Я проверю его и попрошу подтвердить. Текущая база не удаляется — "
              "она будет переименована и останется рядом.",
         [[btn("Отмена", "m:main")]])


def handle_restore_file(cid, m):
    doc = m.get("document")
    if not doc:
        send(cid, "Пришлите файл базы документом (скрепка → Файл). /cancel — отмена.")
        return
    if doc.get("file_size", 0) > 20 * 1024 * 1024:
        send(cid, "Файл больше 20 МБ — Telegram не даст боту его скачать.")
        return
    tmp = os.path.join(os.path.dirname(DB_PATH), f".restore-{uuid.uuid4().hex}.tmp")
    if not download_file(doc["file_id"], tmp):
        send(cid, "Не удалось скачать файл, попробуйте прислать ещё раз.")
        return
    summary, err = validate_db(tmp)
    if err:
        os.remove(tmp)
        send(cid, f"⛔ {esc(err)}\nТекущая база не тронута. Пришлите другой файл или /cancel.")
        return
    STATE[cid] = {"s": "restore_confirm", "tmp": tmp}
    send(cid, f"Файл в порядке: {summary}.\n\nЗаменить текущую базу этим файлом?",
         [[btn("✅ Заменить", "rs:yes"), btn("Отмена", "rs:no")]])

# ───────────────────────────── сообщения преподавателя ─────────────────────────────


def teacher_message(m, text):
    cid = m["chat"]["id"]
    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0].lower()
        st = STATE.pop(cid, None)
        if st and st.get("tmp") and os.path.exists(st["tmp"]):
            os.remove(st["tmp"])
        if cmd == "/backup":
            send(cid, "Готовлю копию…")
            if not backup_now(cid):
                send(cid, "⚠️ Не удалось отправить копию, попробуйте позже.")
        elif cmd == "/restore":
            start_restore(cid)
        else:
            if cmd == "/cancel":
                send(cid, "Отменено.")
            main_menu(cid)
        return
    st = STATE.get(cid)
    if st:
        teacher_state(cid, m, text, st)
    elif m.get("document"):
        send(cid, "Чтобы восстановить базу из файла, сначала отправьте /restore.")
    else:
        main_menu(cid)


def teacher_state(cid, m, text, st):
    s = st["s"]
    if s == "restore":
        handle_restore_file(cid, m)
        return
    if s == "restore_confirm":
        send(cid, "Нажмите «Заменить» или «Отмена» выше, либо /cancel.")
        return
    if s == "hw_files":
        st["files"].append((cid, m["message_id"]))
        send(cid, f"📎 Добавлено вложений: {len(st['files'])}. Пришлите ещё или сохраните.",
             [[btn("💾 Сохранить и отправить", "hwsave")], [btn("Отмена", "hwcancel")]])
        return
    if not text:
        send(cid, "Здесь нужен текст. /cancel — отмена.")
        return
    sid = st.get("sid")

    if s == "hw_text":
        STATE[cid] = {"s": "hw_files", "sid": sid, "lid": st.get("lid"), "text": text, "files": []}
        send(cid, "Текст задания готов. Теперь можно прислать файлы, фото или ссылки — "
                  "каждое отдельным сообщением. Они уйдут ученику следом за текстом.",
             [[btn("💾 Сохранить и отправить", "hwsave")], [btn("Отмена", "hwcancel")]])

    elif s == "add_name":
        name = text[:64]
        sid = x("INSERT INTO students(name, created) VALUES(?,?)", name, today().isoformat())
        STATE.pop(cid, None)
        send(cid, f"Добавлен(а) <b>{esc(name)}</b>. Укажите ставку, длительность и расписание в карточке.")
        teacher_card(cid, sid)

    elif s == "rename":
        x("UPDATE students SET name=? WHERE id=?", text[:64], sid)
        STATE.pop(cid, None)
        teacher_card(cid, sid)

    elif s in ("rate", "dur"):
        digits = "".join(re.findall(r"\d+", text))
        if not digits or (s == "dur" and not 5 <= int(digits) <= 600):
            send(cid, "Нужно число, например 1500 или 60. /cancel — отмена.")
            return
        x(f"UPDATE students SET {'rate' if s == 'rate' else 'duration'}=? WHERE id=?", int(digits), sid)
        STATE.pop(cid, None)
        teacher_card(cid, sid)

    elif s == "pay":
        nums = re.findall(r"\d+", text)
        if len(nums) < 2 or int(nums[0]) == 0:
            send(cid, "Напишите число занятий и сумму, например: <code>4 6000</code>. /cancel — отмена.")
            return
        cnt, amount = int(nums[0]), int("".join(nums[1:]))
        pid = x("INSERT INTO payments(student_id, lessons, amount, date) VALUES(?,?,?,?)",
                sid, cnt, amount, today().isoformat())
        STATE.pop(cid, None)
        stu = student(sid)
        send(cid, f"💳 Оплата: {esc(stu['name'])} — {lessons_word(cnt)} за {money(amount)}.\n"
                  f"{balance_line(stu)}",
             [[btn("↩️ Отменить эту оплату", f"pd:{pid}"), btn("👤 Карточка", f"s:{sid}")]])

    elif s == "slot_time":
        t = parse_time(text)
        if not t:
            send(cid, "Не поняла время. Пример: <code>18:00</code>. /cancel — отмена.")
            return
        x("INSERT INTO slots(student_id, weekday, time, created) VALUES(?,?,?,?)",
          sid, st["wd"], t, today().isoformat())
        STATE.pop(cid, None)
        slots_view(cid, sid)

    elif s == "oneoff":
        d, t = parse_date_time(text, future=True)
        if not d or not t:
            send(cid, "Нужны дата и время, например: <code>03.10 18:00</code>. /cancel — отмена.")
            return
        lid = x("INSERT INTO lessons(student_id, date, time, status, updated) "
                "VALUES(?,?,?,'scheduled',?)", sid, d.isoformat(), t, now().isoformat())
        STATE.pop(cid, None)
        send(cid, "➕ Разовое занятие добавлено.")
        lesson_view(cid, f"L{lid}")

    elif s == "move":
        info = lesson_info(st["key"])
        if not info or info["status"] != "scheduled":
            STATE.pop(cid, None)
            send(cid, "Это занятие уже нельзя перенести.")
            return
        d, t = parse_date_time(text, future=True)
        if d is False:
            send(cid, "Не поняла. Примеры: <code>03.10 18:00</code>, <code>03.10</code> "
                      "или только <code>19:30</code>. /cancel — отмена.")
            return
        new_d = (d or date.fromisoformat(info["date"])).isoformat()
        new_t = t or info["time"]
        lid = materialize(st["key"])
        moved_from = info["moved_from"] or fmt_when(info)
        x("UPDATE lessons SET date=?, time=?, moved_from=?, updated=? WHERE id=?",
          new_d, new_t, moved_from, now().isoformat(), lid)
        STATE.pop(cid, None)
        send(cid, "🔁 Занятие перенесено. Постоянное расписание не изменилось.")
        lesson_view(cid, f"L{lid}")

    elif s == "cancel_reason":
        STATE.pop(cid, None)
        finish_cancel(cid, st["lid"], st["charged"], text)

    elif s == "done_date":
        d = parse_date(text, future=False)
        if not d:
            send(cid, "Не поняла дату. Пример: <code>24.09</code>. /cancel — отмена.")
            return
        STATE.pop(cid, None)
        done_on_date(cid, sid, d)

# ───────────────────────────── кнопки преподавателя ─────────────────────────────


KEEP_STATE = {"hwsave", "hwcancel", "rs", "lnr"}


def teacher_callback(cq, data, cid, mid):
    p = data.split(":")
    a = p[0]
    if a not in KEEP_STATE:
        old = STATE.pop(cid, None)
        if old and old.get("tmp") and os.path.exists(old["tmp"]):
            os.remove(old["tmp"])

    if a == "m":
        w = p[1]
        if w == "main":
            main_menu(cid, mid)
        elif w == "students":
            students_list(cid, mid)
        elif w == "archive":
            students_list(cid, mid, archived=True)
        elif w == "add":
            STATE[cid] = {"s": "add_name"}
            show(cid, "Как зовут нового ученика?", [[btn("Отмена", "m:students")]], mid)
        elif w == "today":
            schedule_view(cid, 1, mid)
        elif w == "week":
            schedule_view(cid, 7, mid)
        elif w == "backup":
            if not backup_now(cid):
                send(cid, "⚠️ Не удалось отправить копию, попробуйте позже.")
        elif w == "restore":
            start_restore(cid)
        return

    if a == "s":
        sid = int(p[1])
        st = student(sid)
        if not st:
            show(cid, "Ученик не найден.", [[btn("⬅️ Ученики", "m:students")]], mid)
            return
        act = p[2] if len(p) > 2 else "card"
        back = [[btn("Отмена", f"s:{sid}")]]
        name = esc(st["name"])
        if act == "card":
            teacher_card(cid, sid, mid)
        elif act == "done":
            show(cid, f"Когда прошло занятие с {name}?",
                 [[btn("Сегодня", f"sd:{sid}:0"), btn("Вчера", f"sd:{sid}:1")],
                  [btn("Другая дата", f"sd:{sid}:other")], [btn("⬅️ Карточка", f"s:{sid}")]], mid)
        elif act == "pay":
            STATE[cid] = {"s": "pay", "sid": sid}
            show(cid, f"💳 Оплата от {name}: сколько занятий и какая сумма?\n"
                      f"Например: <code>4 6000</code>", back, mid)
        elif act == "rate":
            STATE[cid] = {"s": "rate", "sid": sid}
            show(cid, f"Ставка за одно занятие для {name} (сейчас {money(st['rate'])}):", back, mid)
        elif act == "dur":
            STATE[cid] = {"s": "dur", "sid": sid}
            show(cid, f"Длительность занятия в минутах (сейчас {st['duration']}):", back, mid)
        elif act == "name":
            STATE[cid] = {"s": "rename", "sid": sid}
            show(cid, f"Новое имя вместо «{name}»:", back, mid)
        elif act == "slots":
            slots_view(cid, sid, mid)
        elif act == "oneoff":
            STATE[cid] = {"s": "oneoff", "sid": sid}
            show(cid, f"➕ Разовое занятие для {name}: дата и время.\n"
                      f"Например: <code>03.10 18:00</code> или <code>завтра 17:30</code>", back, mid)
        elif act == "lessons":
            student_lessons_view(cid, sid, mid)
        elif act == "hw":
            hw_view(cid, sid, mid)
        elif act == "code":
            code_view(cid, sid, mid)
        elif act == "newcode":
            x("UPDATE students SET invite_code=? WHERE id=?", new_code(), sid)
            code_view(cid, sid, mid)
        elif act == "arch":
            show(cid, f"Убрать {name} в архив? Ученик пропадёт из списков и расписания, "
                      f"все данные сохранятся, вернуть можно в любой момент.",
                 [[btn("📦 Да, в архив", f"s:{sid}:archyes"), btn("Нет", f"s:{sid}")]], mid)
        elif act == "archyes":
            x("UPDATE students SET archived=1 WHERE id=?", sid)
            students_list(cid, mid)
        elif act == "unarch":
            x("UPDATE students SET archived=0 WHERE id=?", sid)
            teacher_card(cid, sid, mid)
        return

    if a == "sd":
        sid = int(p[1])
        if p[2] == "other":
            STATE[cid] = {"s": "done_date", "sid": sid}
            show(cid, "Введите дату занятия, например <code>24.09</code>:",
                 [[btn("Отмена", f"s:{sid}")]], mid)
        else:
            done_on_date(cid, sid, today() - timedelta(days=int(p[2])), mid)
        return

    if a == "slotadd":
        sid = int(p[1])
        kb = [[btn(WD_SHORT[i], f"slotday:{sid}:{i}") for i in range(0, 4)],
              [btn(WD_SHORT[i], f"slotday:{sid}:{i}") for i in range(4, 7)],
              [btn("Отмена", f"s:{sid}:slots")]]
        show(cid, "Выберите день недели:", kb, mid)
        return

    if a == "slotday":
        sid, wd = int(p[1]), int(p[2])
        STATE[cid] = {"s": "slot_time", "sid": sid, "wd": wd}
        show(cid, f"{WD_SHORT[wd]}: во сколько? Например <code>18:00</code>",
             [[btn("Отмена", f"s:{sid}:slots")]], mid)
        return

    if a == "slotdel":
        sl = q1("SELECT * FROM slots WHERE id=?", int(p[1]))
        if sl:
            # будущие занятия этого слота, с которыми ничего не делали, убираем вместе со слотом
            x("DELETE FROM lessons WHERE slot_id=? AND status='scheduled' AND moved_from='' "
              "AND date>=?", sl["id"], today().isoformat())
            x("DELETE FROM slots WHERE id=?", sl["id"])
            slots_view(cid, sl["student_id"], mid)
        return

    if a == "l":
        lesson_view(cid, p[1], mid)
        return

    if a == "ld":
        info = lesson_info(p[1])
        if not info or info["status"] != "scheduled":
            lesson_view(cid, p[1], mid)
            return
        lid = materialize(p[1])
        mark_done(lid)
        done_followup(cid, lid, mid)
        return

    if a == "lm":
        info = lesson_info(p[1])
        if not info:
            return
        STATE[cid] = {"s": "move", "key": p[1]}
        show(cid, f"🔁 Перенос: {esc(info['name'])}, {fmt_when(info)}.\n"
                  f"Новая дата и время, например <code>03.10 18:00</code>, "
                  f"или только время <code>19:30</code>, если в тот же день.",
             [[btn("Отмена", f"l:{p[1]}")]], mid)
        return

    if a == "lc":
        info = lesson_info(p[1])
        if not info:
            return
        show(cid, f"❌ Отмена: {esc(info['name'])}, {fmt_when(info)}.\nСписывать занятие из остатка?",
             [[btn("Со списанием", f"lcc:{p[1]}:1")],
              [btn("Без списания", f"lcc:{p[1]}:0")],
              [btn("⬅️ Назад", f"l:{p[1]}")]], mid)
        return

    if a == "lcc":
        info = lesson_info(p[1])
        if not info or info["status"] != "scheduled":
            return
        lid = materialize(p[1])
        STATE[cid] = {"s": "cancel_reason", "lid": lid, "charged": p[2] == "1"}
        show(cid, "Коротко причина? Напишите одной строкой или пропустите.",
             [[btn("Без причины", "lnr")]], mid)
        return

    if a == "lnr":
        st = STATE.pop(cid, None)
        if st and st["s"] == "cancel_reason":
            show(cid, "Причина не указана.", None, mid)
            finish_cancel(cid, st["lid"], st["charged"], "")
        return

    if a == "lu":
        info = lesson_info(p[1])
        if info and not info["virtual"]:
            x("UPDATE lessons SET status='scheduled', charged=0, note='', updated=? WHERE id=?",
              now().isoformat(), info["id"])
            lesson_view(cid, p[1], mid)
        return

    if a == "pd":
        pay = q1("SELECT * FROM payments WHERE id=?", int(p[1]))
        if pay:
            x("DELETE FROM payments WHERE id=?", pay["id"])
            stu = student(pay["student_id"])
            show(cid, f"Оплата удалена.\n{balance_line(stu)}",
                 [[btn("👤 Карточка", f"s:{stu['id']}")]], mid)
        return

    if a == "hw":
        sid, lid = int(p[1]), int(p[2])
        STATE[cid] = {"s": "hw_text", "sid": sid, "lid": lid or None}
        show(cid, f"✍️ Домашка для {esc(student(sid)['name'])}: напишите текст задания "
                  f"одним сообщением. Файлы и ссылки — на следующем шаге.",
             [[btn("Отмена", f"s:{sid}")]], mid)
        return

    if a == "hwsave":
        st = STATE.pop(cid, None)
        if not st or st["s"] != "hw_files":
            show(cid, "Черновик не найден.", None, mid)
            return
        hid = x("INSERT INTO homework(student_id, lesson_id, text, created) VALUES(?,?,?,?)",
                st["sid"], st["lid"], st["text"], now().isoformat())
        for fc, mi in st["files"]:
            x("INSERT INTO homework_files(homework_id, from_chat_id, message_id) VALUES(?,?,?)",
              hid, fc, mi)
        result = deliver_hw(hid)
        show(cid, f"📚 Домашка сохранена. {result}", [[btn("👤 Карточка", f"s:{st['sid']}")]], mid)
        return

    if a == "hwcancel":
        st = STATE.pop(cid, None)
        show(cid, "Домашка не сохранена.",
             [[btn("👤 Карточка", f"s:{st['sid']}")]] if st and st.get("sid") else None, mid)
        return

    if a == "hwf":      # кнопка из старых сообщений: вложения последнего задания ученика
        hw = latest_hw(int(p[1]))
        if hw:
            for f in hw_files(hw["id"]):
                copy_message(cid, f["from_chat_id"], f["message_id"])
        return

    if a == "hwfi":     # вложения конкретного задания
        for f in hw_files(int(p[1])):
            copy_message(cid, f["from_chat_id"], f["message_id"])
        return

    if a == "hwre":
        hw = latest_hw(int(p[1]))
        if hw:
            send(cid, deliver_hw(hw["id"]))
        return

    if a == "rem":
        stu = student(int(p[1]))
        if stu and stu["tg_chat_id"] and send(stu["tg_chat_id"], esc(reminder_text(stu))):
            show(cid, f"✅ Напоминание об оплате отправлено: {esc(stu['name'])}.", None, mid)
        else:
            send(cid, "⚠️ Не удалось отправить — перешлите текст вручную.")
        return

    if a == "rs":
        st = STATE.pop(cid, None)
        if not st or st["s"] != "restore_confirm" or not os.path.exists(st["tmp"]):
            show(cid, "Нечего восстанавливать — начните заново: /restore", None, mid)
            return
        if p[1] != "yes":
            os.remove(st["tmp"])
            show(cid, "Восстановление отменено, база не тронута.", None, mid)
            return
        try:
            old = do_restore(st["tmp"])
        except Exception as e:
            log.exception("restore")
            show(cid, f"⛔ Не удалось заменить базу: {esc(e)}. Текущая база на месте.", None, mid)
            return
        STATE.clear()
        show(cid, f"✅ База восстановлена.\nПрежняя сохранена рядом как "
                  f"<code>{esc(os.path.basename(old))}</code>.", None, mid)
        main_menu(cid)
        return

# ───────────────────────────── ученик ─────────────────────────────


def student_card(cid, st, mid=None):
    sid = st["id"]
    nl = next_lesson(sid)
    lines = [f"👋 <b>{esc(st['name'])}</b>", "",
             balance_line(st),
             f"Ближайшее занятие: {fmt_when(nl) if nl else '—'}",
             f"Постоянное расписание: {slots_text(sid) or '—'}"]
    kb = [[btn("📅 Занятия на неделю", "st:week")]]
    for i, hw in enumerate(recent_hws(sid)):
        lines += ["", hw_title(i, hw), esc(clip(hw["text"], 1700))]
        files = hw_files(hw["id"])
        if files:
            kb.append([btn(f"📎 Материалы от {fmt_date(hw['created'][:10])} ({len(files)})",
                           f"st:f:{hw['id']}")])
    kb.append([btn("🔄 Обновить", "st:card")])
    show(cid, "\n".join(lines), kb, mid)


def student_week(cid, st, mid=None):
    t = today()
    ls = get_lessons(t, t + timedelta(days=6), st["id"])
    lines = [f"📅 <b>Ваши занятия: {t:%d.%m} – {t + timedelta(days=6):%d.%m}</b>", ""]
    lines += [lesson_line(l, with_date=True, with_name=False) for l in ls] or ["Занятий нет."]
    show(cid, "\n".join(lines), [[btn("⬅️ Назад", "st:card")]], mid)


def student_message(m, text):
    uid, cid = m["from"]["id"], m["chat"]["id"]
    code = None
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        code = parts[1] if len(parts) > 1 else None
    elif text and not text.startswith("/"):
        code = text
    if code:
        c = re.sub(r"[^A-Za-z0-9]", "", code).upper()
        st = q1("SELECT * FROM students WHERE invite_code=? AND archived=0", c) if 4 <= len(c) <= 12 else None
        if st:
            x("UPDATE students SET tg_chat_id=?, invite_code=NULL WHERE id=?", uid, st["id"])
            send(cid, "Готово! Это ваш личный кабинет: здесь расписание, остаток занятий и домашние задания.")
            student_card(cid, student(st["id"]))
            send(ADMIN_ID, f"🔗 {esc(st['name'])} подключился(ась) к боту.")
            return
    st = linked_student(uid)
    if st:
        student_card(cid, st)
    elif code:
        send(cid, "Код не подошёл. Проверьте его или попросите у преподавателя новый.")
    else:
        send(cid, "Здравствуйте! Чтобы открыть личный кабинет, отправьте код приглашения "
                  "от преподавателя.")


def student_callback(data, uid, cid, mid):
    st = linked_student(uid)
    if not st:
        show(cid, "Кабинет не найден. Отправьте код приглашения от преподавателя.", None, mid)
        return
    if data == "st:week":
        student_week(cid, st, mid)
    elif data == "st:files":
        hw = latest_hw(st["id"])
        if hw:
            for f in hw_files(hw["id"]):
                copy_message(cid, f["from_chat_id"], f["message_id"])
    else:
        student_card(cid, st, mid)

# ───────────────────────────── главный цикл ─────────────────────────────


def handle_update(u):
    if "callback_query" in u:
        cq = u["callback_query"]
        api("answerCallbackQuery", {"callback_query_id": cq["id"]})
        msg = cq.get("message")
        if not msg:
            return
        uid, cid, mid = cq["from"]["id"], msg["chat"]["id"], msg["message_id"]
        data = cq.get("data", "")
        if uid == ADMIN_ID:
            teacher_callback(cq, data, cid, mid)
        elif data.startswith("st:"):
            student_callback(data, uid, cid, mid)
    elif "message" in u:
        m = u["message"]
        if m["chat"].get("type") != "private" or "from" not in m:
            return
        text = (m.get("text") or "").strip()
        if m["from"]["id"] == ADMIN_ID:
            teacher_message(m, text)
        else:
            student_message(m, text)


def main():
    global BOT_USERNAME
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_config()
    open_db()
    me = api("getMe")
    if not me:
        print("ОШИБКА: не удалось связаться с Telegram — проверьте TG_BOT_TOKEN.", file=sys.stderr)
        sys.exit(1)
    BOT_USERNAME = me.get("username", "")
    api("deleteWebhook")
    api("setMyCommands", {"commands": [{"command": "start", "description": "Личный кабинет"}]})
    api("setMyCommands", {"scope": {"type": "chat", "chat_id": ADMIN_ID}, "commands": [
        {"command": "start", "description": "Главное меню"},
        {"command": "backup", "description": "Копия базы сейчас"},
        {"command": "restore", "description": "Восстановить базу из файла"},
        {"command": "cancel", "description": "Отменить текущее действие"}]})
    log.info("Бот @%s запущен. База: %s. Копия ежедневно после %02d:00.",
             BOT_USERNAME, DB_PATH, BACKUP_HOUR)
    offset = 0
    while True:
        try:
            maybe_auto_backup()
        except Exception:
            log.exception("Автокопия")
        updates = api("getUpdates", {"offset": offset, "timeout": 25,
                                     "allowed_updates": ["message", "callback_query"]}, timeout=40)
        if updates is None:
            time.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                handle_update(u)
            except Exception:
                log.exception("Ошибка при обработке обновления")
                try:
                    cid = (u.get("message") or (u.get("callback_query") or {}).get("message") or {}) \
                        .get("chat", {}).get("id")
                    if cid:
                        send(cid, "⚠️ Что-то пошло не так. Попробуйте ещё раз или /start.")
                except Exception:
                    pass


if __name__ == "__main__":
    main()
