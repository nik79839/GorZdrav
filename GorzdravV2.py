#!/usr/bin/env python3
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import requests
import vk_api
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from vk_api.longpoll import VkEventType, VkLongPoll
from vk_api.utils import get_random_id

# ============================== НАСТРОЙКИ ==============================
VK_TOKEN = "vk1.a.NtlIW2sPiZihemQK3FFq7gHleSEHsQW-WDnbZMJgXdY_PqjP169r5T_74y_Lmi8NUK_EWNuFedzjnSqodMcd3emNMjF0kvra2qoztMSOo_l6axkfDIKMBFO2B5mPJT6963nFl8TBpF6JqKSg4qYlw4qG2zIE2bxxNZzIRVuaSTMJhzWtoPPzyislISWVfyOu51jmqSH6hKiSZqhfbvomOg"
ALLOWED_USERS = {124970852, 227899871}
POLL_INTERVAL = 30                     
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subscriptions.json")
# =======================================================================

BASE = "https://gorzdrav.spb.ru/_api/api/v2"
MSK = timezone(timedelta(hours=3))
MAX_LIST = 40

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gorzdrav")

vk = None  # VK API, инициализируется в main()


def now_msk() -> datetime:
    return datetime.now(MSK).replace(tzinfo=None)


# ------------------------------ API ГорЗдрава ------------------------------
_http = requests.Session()
_http.headers.update({"User-Agent": "Mozilla/5.0 (gorzdrav-vk-bot)", "Accept": "application/json"})
_static_cache = {}


def api_get(path: str):
    """GET к API. Пустой список, если записей нет; RuntimeError при сетевых сбоях."""
    last_err = None
    for _ in range(3):
        try:
            r = _http.get(BASE + path, timeout=15)
            if r.status_code == 404:
                return []
            r.raise_for_status()
            data = r.json()
            if not data.get("success", True):
                return []
            return data.get("result") or []
        except (requests.RequestException, ValueError) as e:
            last_err = e
            time.sleep(2)
    raise RuntimeError(f"GorZdrav request failed: {path}: {last_err}")


def cached(path: str, ttl: int = 1800):
    item = _static_cache.get(path)
    if item and time.time() - item[0] < ttl:
        return item[1]
    data = api_get(path)
    _static_cache[path] = (time.time(), data)
    return data


def enc(value) -> str:
    """'п.139' -> '%D0%BF%2e139' (точка тоже кодируется)."""
    return urllib.parse.quote(str(value), safe="").replace(".", "%2e")


def fetch_doctors(lpu_id, spec_id):
    return api_get(f"/schedule/lpu/{lpu_id}/speciality/{enc(spec_id)}/doctors")


def fetch_appointments(lpu_id, doctor_id):
    return api_get(f"/schedule/lpu/{lpu_id}/doctor/{enc(doctor_id)}/appointments")


# ------------------------------ Хранилище ------------------------------
class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def list(self, uid):
        with self.lock:
            return deepcopy(self.data.get(str(uid), []))

    def snapshot(self):
        with self.lock:
            return [(int(u), deepcopy(s)) for u, subs in self.data.items() for s in subs]

    def add(self, uid, sub) -> bool:
        """True, если подписка на этого врача уже была и просто обновлена."""
        with self.lock:
            subs = self.data.setdefault(str(uid), [])
            for i, s in enumerate(subs):
                if s["lpu_id"] == sub["lpu_id"] and s["doctor_id"] == sub["doctor_id"]:
                    subs[i] = sub
                    self._save()
                    return True
            subs.append(sub)
            self._save()
            return False

    def remove(self, uid, sub_id) -> bool:
        with self.lock:
            subs = self.data.get(str(uid), [])
            new = [s for s in subs if s["id"] != sub_id]
            self.data[str(uid)] = new
            self._save()
            return len(new) != len(subs)

    def update(self, uid, sub_id, **fields):
        with self.lock:
            for s in self.data.get(str(uid), []):
                if s["id"] == sub_id:
                    s.update(fields)
                    self._save()
                    return


store = Store(DATA_FILE)


# ------------------------------ Проверка подписок ------------------------------
def fmt_date(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d.%m.%Y")


def fmt_dt(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M")


def has_range(sub) -> bool:
    return bool(sub.get("date_from") or sub.get("date_to"))


def range_text(sub) -> str:
    if not has_range(sub):
        return "любой (сообщу, когда появится запись или станет ближе)"
    f = fmt_date(sub["date_from"]) if sub.get("date_from") else "…"
    t = fmt_date(sub["date_to"]) if sub.get("date_to") else "…"
    return f"{f} — {t}"


def doctor_info(sub, cache):
    key = ("doc", sub["lpu_id"], sub["spec_id"])
    if key not in cache:
        cache[key] = fetch_doctors(sub["lpu_id"], sub["spec_id"])
    for d in cache[key]:
        if d["id"] == sub["doctor_id"]:
            return d
    return None


def window_slots(sub, cache):
    key = ("app", sub["lpu_id"], sub["doctor_id"])
    if key not in cache:
        cache[key] = fetch_appointments(sub["lpu_id"], sub["doctor_id"])
    d_from = date.fromisoformat(sub["date_from"]) if sub.get("date_from") else None
    d_to = date.fromisoformat(sub["date_to"]) if sub.get("date_to") else None
    now = now_msk()
    out = []
    for a in cache[key]:
        dt = datetime.fromisoformat(a["visitStart"])
        if dt < now or (d_from and dt.date() < d_from) or (d_to and dt.date() > d_to):
            continue
        out.append(a)
    out.sort(key=lambda a: a["visitStart"])
    return out


def sub_header(sub) -> str:
    return f"{sub['doctor_name']}\n{sub['lpu_name']}\n{sub['spec_name']}"


def slots_lines(slots, limit=10):
    lines = []
    for a in slots[:limit]:
        place = ", ".join(x for x in [f"каб. {a['room']}" if a.get("room") else "", a.get("address") or ""] if x)
        lines.append(f"• {fmt_dt(a['visitStart'])}" + (f" ({place})" if place else ""))
    if len(slots) > limit:
        lines.append(f"…и ещё {len(slots) - limit}")
    return "\n".join(lines)


def check_sub(sub, cache):
    """Возвращает (текст уведомления | None, обновления состояния подписки)."""
    if has_range(sub):
        slots = window_slots(sub, cache)
        seen = set(sub.get("seen", []))
        new = [a for a in slots if a["id"] not in seen]
        upd = {"seen": [a["id"] for a in slots]}
        if not new:
            return None, upd
        msg = (f"🔔 Новые талоны в вашем промежутке ({range_text(sub)})!\n\n{sub_header(sub)}\n\n"
               f"{slots_lines(new)}\n\nЗапись: https://gorzdrav.spb.ru/service-free-schedule")
        return msg, upd

    info = doctor_info(sub, cache)
    if info is None:
        return None, {}
    nearest = info.get("nearestDate") if info.get("freeTicketCount", 0) > 0 else None
    last = sub.get("last_nearest")
    upd = {"last_nearest": nearest}
    if nearest and (last is None or nearest < last):
        msg = (f"🔔 Появилась запись!\n\n{sub_header(sub)}\n\nБлижайшая дата: {fmt_date(nearest)} "
               f"(свободных талонов: {info.get('freeTicketCount')})\n\n"
               f"Запись: https://gorzdrav.spb.ru/service-free-schedule")
        return msg, upd
    return None, upd


def baseline(sub) -> str:
    """Запоминает текущее состояние (чтобы не слать уже известное) и возвращает описание."""
    cache = {}
    _, upd = check_sub(sub, cache)
    sub.update(upd)
    if has_range(sub):
        slots = window_slots(sub, cache)
        if not slots:
            return "В выбранном промежутке сейчас свободных талонов нет."
        return f"В промежутке сейчас талонов: {len(slots)}, ближайший — {fmt_dt(slots[0]['visitStart'])}."
    info = doctor_info(sub, cache)
    if info is None:
        return "Врача сейчас нет в списке расписания, но я продолжу следить."
    if info.get("freeTicketCount", 0) > 0 and info.get("nearestDate"):
        return f"Ближайшая запись сейчас: {fmt_date(info['nearestDate'])} (талонов: {info['freeTicketCount']})."
    return "Свободных талонов сейчас нет."


def poll_once():
    cache = {}
    for uid, sub in store.snapshot():
        try:
            msg, upd = check_sub(sub, cache)
            if msg:
                send(uid, msg)  # при ошибке отправки состояние не обновится -> повтор в след. цикл
            if upd:
                store.update(uid, sub["id"], **upd)
        except Exception as e:
            log.warning("poll error for %s/%s: %s", uid, sub.get("doctor_name"), e)


def poller():
    while True:
        started = time.time()
        try:
            poll_once()
        except Exception:
            log.exception("poll cycle failed")
        time.sleep(max(1.0, POLL_INTERVAL - (time.time() - started)))


# ------------------------------ VK: отправка ------------------------------
def kb_main():
    kb = VkKeyboard(one_time=False)
    kb.add_button("Подписаться", VkKeyboardColor.PRIMARY)
    kb.add_button("Мои подписки")
    kb.add_line()
    kb.add_button("Отписаться", VkKeyboardColor.NEGATIVE)
    return kb


def kb_cancel():
    kb = VkKeyboard(one_time=False)
    kb.add_button("Отмена", VkKeyboardColor.NEGATIVE)
    return kb


def send(uid, text, kb=None):
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for i, chunk in enumerate(chunks):
        params = {"user_id": uid, "message": chunk, "random_id": get_random_id()}
        if kb is not None and i == len(chunks) - 1:
            params["keyboard"] = kb.get_keyboard()
        vk.messages.send(**params)


MENU = (
    "Я слежу за свободными талонами к врачам ГорЗдрава СПб (проверка раз в 30 секунд).\n\n"
    "• Подписаться — пошаговый выбор: район → поликлиника → специализация → врач → промежуток дат\n"
    "• Поиск <запрос> — быстрый поиск, например: поиск Адмиралтейский 111 терапевт Иванова\n"
    "• Мои подписки — список подписок\n"
    "• Отписаться [номер|все]\n"
    "• Промежуток <номер> <даты> — изменить промежуток, например: промежуток 1 01.11-10.11\n"
    "• Отмена — выйти из текущего диалога"
)

# ------------------------------ Поиск / фильтрация ------------------------------
def norm(s: str) -> str:
    return s.lower().replace("ё", "е")


def tokens(s: str):
    return [w for w in re.split(r"[\s,;.]+", norm(s).replace("№", "")) if w]


def stem(t: str) -> str:
    return t if t.isdigit() else t[:max(3, len(t) - 2)]


def matches(hint: str, text: str) -> bool:
    return stem(hint) in norm(text)


def apply_hints(items, hints):
    """Фильтрует варианты по словам запроса. Возвращает (варианты, оставшиеся слова, было ли совпадение)."""
    used = [h for h in hints if any(matches(h, it["text"]) for it in items)]
    if not used:
        return items, hints, False
    res = [it for it in items if all(matches(h, it["text"]) for h in used)]
    if not res:
        res = [it for it in items if any(matches(h, it["text"]) for h in used)]
    return res, [h for h in hints if h not in used], True


# ------------------------------ Мастер подписки ------------------------------
STAGES = ["district", "lpu", "spec", "doctor"]
TITLES = {"district": "Выберите район", "lpu": "Выберите поликлинику",
          "spec": "Выберите специализацию", "doctor": "Выберите врача"}
STATE = {}  # uid -> {"stage", "ctx", "hints", "options"}; трогается только из потока longpoll


def short_name(name: str, keep_note: bool = False) -> str:
    """'Иванова Мария Петровна (нет приема 1.11)' -> 'Иванова М.П.'"""
    m = re.search(r"\s*(\(.*\))\s*$", name)
    note = m.group(1) if m else ""
    parts = (name[:m.start()] if m else name).split()
    if len(parts) >= 3:
        res = f"{parts[0]} {parts[1][0]}.{parts[2][0]}."
    elif len(parts) == 2:
        res = f"{parts[0]} {parts[1][0]}."
    else:
        res = name.strip()
    return f"{res} {note}" if keep_note and note else res


def lpu_label(l) -> str:
    return l.get("lpuShortName") or l["lpuFullName"]


def items_for(stage, ctx):
    if stage == "district":
        return [{"label": d["name"], "text": d["name"], "val": d} for d in cached("/shared/districts")]
    if stage == "lpu":
        res = cached(f"/shared/district/{ctx['district']['id']}/lpus")
        return [{"label": lpu_label(l),
                 "text": " ".join(filter(None, [l["lpuFullName"], l.get("lpuShortName"),
                                                l.get("address"), l.get("description")])),
                 "val": l} for l in res if l.get("isActive", True)]
    if stage == "spec":
        res = cached(f"/schedule/lpu/{ctx['lpu']['id']}/specialties", ttl=300)
        return [{"label": s["name"], "text": s["name"], "val": s} for s in res]
    if stage == "doctor":
        res = fetch_doctors(ctx["lpu"]["id"], ctx["spec"]["id"])
        out = []
        for d in res:
            if d.get("freeTicketCount", 0) > 0 and d.get("nearestDate"):
                extra = f" — ближайшая {fmt_date(d['nearestDate'])}"
            else:
                extra = " — нет талонов"
            out.append({"label": short_name(d["name"], keep_note=True) + extra, "text": d["name"], "val": d})
        return out
    return []


def start_wizard(uid, hints):
    STATE[uid] = {"stage": "district", "ctx": {}, "hints": hints, "options": []}
    run_stage(uid)


def run_stage(uid, typed=False):
    st = STATE[uid]
    while True:
        stage = st["stage"]
        if stage == "range":
            send(uid, "Укажите промежуток дат для записи, например «01.11-10.11», «с 05.11» или «до 20.11».\n"
                      "Напишите «любой», если ограничений нет — тогда сообщу, когда появится запись "
                      "или найдётся более ранняя.", kb_cancel())
            return
        items = items_for(stage, st["ctx"])
        if not items:
            STATE.pop(uid, None)
            send(uid, f"{TITLES[stage]}: вариантов не найдено. Попробуйте начать заново.", kb_main())
            return
        shown, st["hints"], matched = apply_hints(items, st["hints"])
        if typed and not matched:
            send(uid, "Совпадений не нашёл, показываю полный список.")
        typed = False
        if len(shown) == 1:
            pick(uid, shown[0])
            continue
        st["options"] = shown
        lines = [f"{i}. {it['label']}" for i, it in enumerate(shown[:MAX_LIST], 1)]
        if len(shown) > MAX_LIST:
            lines.append(f"…и ещё {len(shown) - MAX_LIST}. Уточните запрос словами.")
        send(uid, f"{TITLES[stage]}:\n" + "\n".join(lines) +
             "\n\nОтправьте номер или часть названия для поиска.", kb_cancel())
        return


def pick(uid, item):
    st = STATE[uid]
    stage = st["stage"]
    st["ctx"][stage] = item["val"]
    send(uid, f"✔ {item['label']}")
    i = STAGES.index(stage)
    st["stage"] = STAGES[i + 1] if i + 1 < len(STAGES) else "range"


def wizard_reply(uid, text):
    st = STATE[uid]
    if st["stage"] == "range":
        try:
            d_from, d_to = parse_range(text)
        except ValueError as e:
            send(uid, f"{e}\nПример: 01.11-10.11")
            return
        finish_subscription(uid, d_from, d_to)
        return
    t = text.strip()
    if t.isdigit():
        n = int(t)
        if 1 <= n <= len(st["options"]):
            pick(uid, st["options"][n - 1])
            run_stage(uid)
        else:
            send(uid, "Нет варианта с таким номером.")
        return
    st["hints"] = tokens(t)
    run_stage(uid, typed=True)


def finish_subscription(uid, d_from, d_to):
    st = STATE.pop(uid)
    c = st["ctx"]
    sub = {
        "id": str(int(time.time() * 1000)),
        "lpu_id": c["lpu"]["id"], "lpu_name": lpu_label(c["lpu"]),
        "spec_id": c["spec"]["id"], "spec_name": c["spec"]["name"],
        "doctor_id": c["doctor"]["id"], "doctor_name": short_name(c["doctor"]["name"]),
        "date_from": d_from.isoformat() if d_from else None,
        "date_to": d_to.isoformat() if d_to else None,
        "seen": [], "last_nearest": None,
    }
    try:
        info = baseline(sub)
    except Exception as e:
        log.warning("baseline failed: %s", e)
        send(uid, "Не удалось связаться с ГорЗдравом, попробуйте ещё раз позже.", kb_main())
        return
    replaced = store.add(uid, sub)
    send(uid, f"{'Подписка обновлена' if replaced else 'Подписка оформлена'} ✅\n\n{sub_header(sub)}\n"
              f"Промежуток: {range_text(sub)}\n\n{info}", kb_main())


# ------------------------------ Разбор дат ------------------------------
def parse_one_date(s: str, today: date) -> date:
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?", s.strip())
    if not m:
        raise ValueError(f"Не понял дату «{s.strip()}».")
    d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if y:
            year = int(y) + (2000 if len(y) == 2 else 0)
            return date(year, mo, d)
        res = date(today.year, mo, d)
        return res if res >= today else date(today.year + 1, mo, d)
    except ValueError:
        raise ValueError(f"Такой даты не существует: «{s.strip()}».")


def parse_range(text: str):
    t = norm(text.strip())
    if t in ("любой", "любая", "нет", "все", "без ограничений", "-", "–"):
        return None, None
    t = re.sub(r"^с\s+", "", t)
    t = re.sub(r"^до\s+", "-", t)
    parts = re.split(r"\s*(?:-|–|—|\bпо\b)\s*", t)
    if len(parts) != 2 or not (parts[0] or parts[1]):
        raise ValueError("Не понял промежуток.")
    today = now_msk().date()
    d_from = parse_one_date(parts[0], today) if parts[0] else None
    d_to = parse_one_date(parts[1], today) if parts[1] else None
    if d_from and d_to and d_to < d_from:
        if not re.search(r"\d{4}", parts[1]):
            d_to = d_to.replace(year=d_to.year + 1)
        else:
            raise ValueError("Конец промежутка раньше начала.")
    return d_from, d_to


# ------------------------------ Команды ------------------------------
def show_subs(uid):
    subs = store.list(uid)
    if not subs:
        send(uid, "Подписок пока нет. Нажмите «Подписаться».", kb_main())
        return subs
    blocks = [f"{i}. {s['doctor_name']}\n   {s['lpu_name']} · {s['spec_name']}\n   Промежуток: {range_text(s)}"
              for i, s in enumerate(subs, 1)]
    send(uid, "Ваши подписки:\n\n" + "\n\n".join(blocks), kb_main())
    return subs


def do_unsub(uid, n):
    subs = store.list(uid)
    if not (1 <= n <= len(subs)):
        send(uid, "Нет подписки с таким номером.")
        return
    store.remove(uid, subs[n - 1]["id"])
    STATE.pop(uid, None)
    send(uid, f"Отписал: {subs[n - 1]['doctor_name']}", kb_main())


def cmd_unsub(uid, rest):
    rest = rest.strip().lower()
    if rest == "все":
        for s in store.list(uid):
            store.remove(uid, s["id"])
        send(uid, "Все подписки удалены.", kb_main())
    elif rest.isdigit():
        do_unsub(uid, int(rest))
    elif show_subs(uid):
        STATE[uid] = {"stage": "unsub"}
        send(uid, "Отправьте номер подписки, от которой отписаться.", kb_cancel())


def cmd_range(uid, rest):
    m = re.match(r"(\d+)\s+(.+)", rest.strip())
    subs = store.list(uid)
    if not m or not (1 <= int(m.group(1)) <= len(subs)):
        send(uid, "Формат: промежуток <номер подписки> <даты>, например: промежуток 1 01.11-10.11")
        return
    sub = subs[int(m.group(1)) - 1]
    try:
        d_from, d_to = parse_range(m.group(2))
    except ValueError as e:
        send(uid, str(e))
        return
    sub["date_from"] = d_from.isoformat() if d_from else None
    sub["date_to"] = d_to.isoformat() if d_to else None
    sub["seen"], sub["last_nearest"] = [], None
    try:
        info = baseline(sub)
    except Exception as e:
        log.warning("baseline failed: %s", e)
        send(uid, "Не удалось связаться с ГорЗдравом, попробуйте позже.")
        return
    store.update(uid, sub["id"], date_from=sub["date_from"], date_to=sub["date_to"],
                 seen=sub["seen"], last_nearest=sub["last_nearest"])
    send(uid, f"Промежуток обновлён: {range_text(sub)}\n{info}", kb_main())


def handle(uid, text):
    text = text.strip()
    low = norm(text)
    word, _, rest = text.partition(" ")
    word = norm(word)
    st = STATE.get(uid)

    if low in ("отмена", "меню", "начать", "/start", "start", "помощь"):
        STATE.pop(uid, None)
        send(uid, MENU, kb_main())
    elif low in ("мои подписки", "подписки"):
        STATE.pop(uid, None)
        show_subs(uid)
    elif word == "отписаться":
        STATE.pop(uid, None)
        cmd_unsub(uid, rest)
    elif word == "промежуток":
        cmd_range(uid, rest)
    elif word in ("подписаться", "поиск", "найти"):
        start_wizard(uid, tokens(rest))
    elif st and st["stage"] == "unsub":
        if text.isdigit():
            do_unsub(uid, int(text))
        else:
            send(uid, "Отправьте номер подписки или «Отмена».")
    elif st:
        wizard_reply(uid, text)
    else:
        send(uid, MENU, kb_main())


# ------------------------------ main ------------------------------
def main():
    global vk
    session = vk_api.VkApi(token=VK_TOKEN)
    vk = session.get_api()
    threading.Thread(target=poller, daemon=True).start()
    log.info("Бот запущен (pid %s), опрос каждые %s с", os.getpid(), POLL_INTERVAL)
    recent_ids = deque(maxlen=500)  # защита от повторной доставки одного и того же сообщения

    while True:
        try:
            longpoll = VkLongPoll(session)
            for event in longpoll.listen():
                if event.type != VkEventType.MESSAGE_NEW or not event.to_me or not event.text:
                    continue
                if event.user_id not in ALLOWED_USERS:
                    continue
                if event.message_id in recent_ids:
                    log.info("duplicate event %s skipped", event.message_id)
                    continue
                recent_ids.append(event.message_id)
                try:
                    handle(event.user_id, event.text)
                except Exception:
                    log.exception("handler error")
                    try:
                        send(event.user_id, "Что-то пошло не так, попробуйте ещё раз.", kb_main())
                    except Exception:
                        pass
        except Exception:
            log.exception("longpoll failed, reconnecting")
            time.sleep(5)


if __name__ == "__main__":
    main()