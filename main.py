#!/usr/bin/env python3
"""
VK-бот: подписка на врачей ГорЗдрава СПб и уведомления о появлении записи.

Запуск:  VK_TOKEN=xxxx python gorzdrav_bot.py
"""
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib3
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
POLL_INTERVAL = 30
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subscriptions.json")
PROFILE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json")
MAX_SUBS_PER_USER = 5  # максимум подписок на пользователя
CONSENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "consents.json")
CONSENT_VERSION = 1  # при изменении текста согласия увеличьте: бот запросит согласие заново
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
_http.verify = False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
#_http.proxies = {"http": "direct", "https": "direct"}
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


class PatientNotFound(Exception):
    pass


def api_call(method: str, path: str, **kw) -> dict:
    """Запрос без повторов (для POST повтор небезопасен). Возвращает JSON-ответ как dict."""
    r = _http.request(method, BASE + path, timeout=20, **kw)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if r.status_code >= 400 and not data:
        raise RuntimeError(f"HTTP {r.status_code} {path}")
    return data


def find_patient(lpu_id, prof) -> str:
    """Проверяет, есть ли пациент в поликлинике; возвращает patientId для записи."""
    data = api_call("GET", "/patient/search", params={
        "lpuId": str(lpu_id),
        "lastName": prof["last"],
        "firstName": prof["first"],
        "middleName": prof.get("middle") or "",
        "birthdate": f"{prof['birth']}T00:00:00",
        "birthdateValue": datetime.fromisoformat(prof["birth"]).strftime("%d.%m.%Y"),
    })
    if data.get("success") and data.get("result"):
        return data["result"]
    raise PatientNotFound(data.get("message") or "пациент не найден")


def create_appointment(lpu_id, patient_id, prof, appt):
    """Возвращает (успех, сообщение сайта, errorCode)."""
    payload = {
        "esiaId": None,
        "lpuId": str(lpu_id),
        "patientId": patient_id,
        "appointmentId": appt["id"],
        "referralId": None,
        "ipmpiCardId": None,
        "recipientEmail": prof.get("email") or None,
        "patientLastName": prof["last"],
        "patientFirstName": prof["first"],
        "patientMiddleName": prof.get("middle") or "",
        "patientBirthdate": f"{prof['birth']}T00:00:00",
        "room": appt.get("room"),
        "address": appt.get("address"),
        "visitDate": appt["visitStart"],
    }
    data = api_call("POST", "/appointment/create", json=payload)
    return bool(data.get("success")), (data.get("message") or ""), data.get("errorCode")


# Ошибки, при которых повторять запись на другие талоны бессмысленно
# 636 - нет прикрепления к медорганизации или участку
NO_ATTACHMENT_CODES = {636}


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


class Profiles:
    """Карточки пациентов: {uid: {last, first, middle, birth(ISO), email}}."""

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

    def get(self, uid):
        with self.lock:
            return deepcopy(self.data.get(str(uid)))

    def set(self, uid, prof):
        with self.lock:
            self.data[str(uid)] = prof
            self._save()

    def delete(self, uid) -> bool:
        with self.lock:
            had = self.data.pop(str(uid), None) is not None
            self._save()
            return had


profiles = Profiles(PROFILE_FILE)
consents = Profiles(CONSENT_FILE)  # {uid: {"at": ..., "version": N}}


def has_consent(uid) -> bool:
    c = consents.get(uid)
    return bool(c and c.get("version") == CONSENT_VERSION)


# ------------------------------ Проверка подписок ------------------------------
def fmt_date(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d.%m.%Y")


def fmt_dt(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M")


def has_range(sub) -> bool:
    return bool(sub.get("date_from") or sub.get("date_to"))


def has_time(sub) -> bool:
    return bool(sub.get("time_from") or sub.get("time_to"))


def uses_slots(sub) -> bool:
    """Нужны ли отдельные талоны (а не только ближайшая дата врача)."""
    return has_range(sub) or has_time(sub)


def time_text(sub) -> str:
    f, t = sub.get("time_from"), sub.get("time_to")
    if f and t:
        return f"{f}–{t}"
    return f"после {f}" if f else (f"до {t}" if t else "любое")


def filter_text(sub) -> str:
    return range_text(sub) + (f", время: {time_text(sub)}" if has_time(sub) else "")


def range_text(sub) -> str:
    if not has_range(sub):
        return "любая дата" if has_time(sub) else "любой (сообщу, когда появится запись или станет ближе)"
    if sub.get("date_from") and sub.get("date_from") == sub.get("date_to"):
        return fmt_date(sub["date_from"])
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
        tm = dt.strftime("%H:%M")
        if (sub.get("time_from") and tm < sub["time_from"]) or (sub.get("time_to") and tm >= sub["time_to"]):
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


def slot_offer(a):
    return {k: a.get(k) for k in ("id", "visitStart", "room", "address")}


def check_sub(sub, cache):
    """Возвращает (текст уведомления | None, обновления состояния, предложенные талоны)."""
    if uses_slots(sub):
        slots = window_slots(sub, cache)
        seen = set(sub.get("seen", []))
        new = [a for a in slots if a["id"] not in seen]
        upd = {"seen": [a["id"] for a in slots]}
        if not new:
            return None, upd, []
        offers = [slot_offer(a) for a in new[:5]]
        upd["offers"] = offers
        msg = (f"🔔 Новые талоны ({filter_text(sub)})!\n\n{sub_header(sub)}\n\n"
               f"{slots_lines(new)}\n\nЗапись: {schedule_link(sub)}")
        return msg, upd, offers

    info = doctor_info(sub, cache)
    if info is None:
        return None, {}, []
    nearest = info.get("nearestDate") if info.get("freeTicketCount", 0) > 0 else None
    last = sub.get("last_nearest")
    upd = {"last_nearest": nearest}
    if nearest and (last is None or nearest < last):
        try:
            offers = [slot_offer(a) for a in window_slots(sub, cache)[:5]]
        except Exception:
            offers = []
        upd["offers"] = offers
        msg = (f"🔔 Появилась запись!\n\n{sub_header(sub)}\n\nБлижайшая дата: {fmt_date(nearest)} "
               f"(свободных талонов: {info.get('freeTicketCount')})\n\n"
               f"Запись: {schedule_link(sub)}")
        return msg, upd, offers
    return None, upd, []


def baseline(sub) -> str:
    """Запоминает текущее состояние (чтобы не слать уже известное) и возвращает описание."""
    cache = {}
    _, upd, _ = check_sub(sub, cache)
    upd.pop("offers", None)
    sub.update(upd)
    if uses_slots(sub):
        slots = window_slots(sub, cache)
        if not slots:
            return "По вашим условиям сейчас свободных талонов нет."
        return f"По вашим условиям сейчас талонов: {len(slots)}, ближайший — {fmt_dt(slots[0]['visitStart'])}."
    info = doctor_info(sub, cache)
    if info is None:
        return "Врача сейчас нет в списке расписания, но я продолжу следить."
    if info.get("freeTicketCount", 0) > 0 and info.get("nearestDate"):
        return f"Ближайшая запись сейчас: {fmt_date(info['nearestDate'])} (талонов: {info['freeTicketCount']})."
    return "Свободных талонов сейчас нет."


def booked_text(sub, appt) -> str:
    place = ", ".join(x for x in [f"каб. {appt['room']}" if appt.get("room") else "", appt.get("address") or ""] if x)
    return (f"✅ Вы записаны!\n\n{sub_header(sub)}\n{fmt_dt(appt['visitStart'])}" + (f"\n{place}" if place else "") +
            "\n\nПодписка снята. Отменить или перенести запись можно на сайте ГорЗдрава или в поликлинике.")


MAX_FAILED_SLOTS = 6


def try_auto_book(uid, sub, cache) -> bool:
    """Автозапись на самый ранний талон. True - записали (подписка снята)."""
    prof = profiles.get(uid)
    if not prof:
        return False
    failed = set(sub.get("failed", []))
    slots = [a for a in window_slots(sub, cache) if a["id"] not in failed]
    if not slots:
        return False
    try:
        patient_id = find_patient(sub["lpu_id"], prof)
    except PatientNotFound as e:
        store.update(uid, sub["id"], auto_book=False)
        sub["auto_book"] = False
        send(uid, f"⚠ Автозапись отключена: в поликлинике не найден пациент ({e}). "
                  f"Уведомления продолжатся.\n\n{sub_header(sub)}")
        return False
    last_err = ""
    for a in slots[:3]:
        ok, err, code = create_appointment(sub["lpu_id"], patient_id, prof, a)
        if ok:
            store.remove(uid, sub["id"])
            send(uid, booked_text(sub, a), kb_main())
            return True
        if code in NO_ATTACHMENT_CODES:
            store.update(uid, sub["id"], auto_book=False)
            sub["auto_book"] = False
            send(uid, f"⚠ Автозапись отключена: {err}\n\nУведомления о талонах продолжатся.\n\n{sub_header(sub)}")
            return False
        failed.add(a["id"])
        last_err = err
    upd = {"failed": list(failed)}
    note = ""
    if len(failed) >= MAX_FAILED_SLOTS:
        upd["auto_book"] = False
        sub["auto_book"] = False
        note = " Автозапись отключена, остаются уведомления."
    store.update(uid, sub["id"], **upd)
    send(uid, f"⚠ Автозапись не удалась: {last_err or 'сайт отклонил запись'}.{note}\n\n{sub_header(sub)}")
    return False


def poll_once():
    cache = {}
    for uid, sub in store.snapshot():
        if not has_consent(uid):
            ask_consent_once(uid)
            continue
        try:
            if sub.get("auto_book") and try_auto_book(uid, sub, cache):
                continue
            msg, upd, offers = check_sub(sub, cache)
            if msg:
                if offers:
                    msg += ("\n\nЗаписаться можно кнопкой ниже"
                            + ("" if profiles.get(uid) else " (сначала заполните «Карточку»)") + ".")
                send(uid, msg, offers_kb(sub, offers))  # при ошибке отправки состояние не обновится -> повтор
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
    kb.add_button("Карточка")
    return kb


def kb_time():
    kb = VkKeyboard(one_time=False)
    kb.add_button("Любое время", VkKeyboardColor.PRIMARY)
    kb.add_line()
    kb.add_button("Отмена", VkKeyboardColor.NEGATIVE)
    return kb


def kb_consent():
    kb = VkKeyboard(one_time=False)
    kb.add_button("Согласен", VkKeyboardColor.POSITIVE)
    kb.add_line()
    kb.add_button("Не согласен", VkKeyboardColor.NEGATIVE)
    return kb


def kb_date():
    kb = VkKeyboard(one_time=False)
    kb.add_button("Любая дата", VkKeyboardColor.PRIMARY)
    kb.add_line()
    kb.add_button("Отмена", VkKeyboardColor.NEGATIVE)
    return kb


def kb_yesno():
    kb = VkKeyboard(one_time=False)
    kb.add_button("Да, автозапись", VkKeyboardColor.POSITIVE)
    kb.add_button("Нет, только уведомлять", VkKeyboardColor.SECONDARY)
    return kb


def offers_kb(sub, offers):
    """Inline-кнопки «Записаться» под уведомлением (до 5 талонов)."""
    if not offers:
        return None
    kb = VkKeyboard(inline=True)
    surname = sub["doctor_name"].split()[0]
    for i, a in enumerate(offers[:5]):
        if i:
            kb.add_line()
        stamp = datetime.fromisoformat(a["visitStart"]).strftime("%d.%m %H:%M")
        kb.add_button(f"Записаться {stamp} {surname}"[:40], VkKeyboardColor.POSITIVE)
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


CONSENT_TEXT = (
    "Согласие на обработку персональных данных\n\n"
    "Оператор: Отрашевский Никита Александрович.\n\n"
    "Нажимая «Согласен», вы разрешаете обрабатывать:\n"
    "• ваш идентификатор ВКонтакте;\n"
    "• сведения о подписках: поликлиника, специализация, врач, желаемые дата и время "
    "(они могут косвенно отражать состояние здоровья);\n"
    "• по вашему желанию, в «Карточке»: ФИО, дату рождения и e-mail.\n\n"
    "Цели: присылать уведомления о свободных талонах и записывать вас на приём через сайт "
    "gorzdrav.spb.ru по вашей команде или при включённой автозаписи. Для записи ФИО и дата рождения "
    "передаются в ГорЗдрав.\n"
    "Действия: сбор, хранение, использование, передача в ГорЗдрав для записи, удаление. "
    "Другим лицам данные не передаются.\n"
    "Срок: до отзыва согласия.\n"
    "Отзыв: команда «отозвать согласие», все ваши данные будут удалены."
)

_consent_asked = set()


def ask_consent_once(uid):
    """Для подписчиков, оформленных до появления согласия: один раз просим согласие по инициативе бота."""
    if uid in _consent_asked:
        return
    _consent_asked.add(uid)
    try:
        send(uid, "Уведомления приостановлены: нужно ваше согласие на обработку данных. "
                  "Нажмите «Согласен», и всё продолжится.\n\n" + CONSENT_TEXT, kb_consent())
    except Exception as e:
        log.warning("consent request failed for %s: %s", uid, e)


def consent_gate(uid, low):
    if low.startswith("не согласен") or low in ("нет", "отказываюсь", "отказ"):
        send(uid, "Без согласия бот не может работать, ничего не сохранено. "
                  "Если передумаете, напишите «Начать».", kb_consent())
    elif low.startswith("согласен") or low.startswith("согласна") or low == "да":
        consents.set(uid, {"at": now_msk().isoformat(timespec="seconds"), "version": CONSENT_VERSION})
        send(uid, "Спасибо, согласие сохранено ✅\n\n" + MENU, kb_main())
    else:
        send(uid, CONSENT_TEXT, kb_consent())


def grandfather_existing_users():
    """Пользователи, у которых уже были подписки до появления согласия, считаются согласившимися.
    Только если записи о согласии нет вовсе (повышение CONSENT_VERSION не обходится)."""
    n = 0
    for uid, _ in store.snapshot():
        if consents.get(uid) is None:
            consents.set(uid, {"at": now_msk().isoformat(timespec="seconds"),
                               "version": CONSENT_VERSION, "legacy": True})
            n += 1
    if n:
        log.info("Согласие проставлено существующим пользователям: %s", n)


def cmd_revoke(uid):
    """Отзыв согласия: удаляем подписки, карточку и само согласие."""
    STATE.pop(uid, None)
    for s_ in store.list(uid):
        store.remove(uid, s_["id"])
    profiles.delete(uid)
    consents.delete(uid)
    _consent_asked.add(uid)
    send(uid, "Согласие отозвано, все ваши данные удалены: подписки и карточка. "
              "Чтобы снова пользоваться ботом, напишите «Начать».", kb_consent())


MENU = (
    "Я слежу за свободными талонами к врачам ГорЗдрава СПб (проверка раз в 30 секунд).\n\n"
    "• Подписаться — пошаговый выбор: район → поликлиника → специализация → врач → промежуток дат\n"
    "• Поиск <запрос> — быстрый поиск, например: поиск Адмиралтейский 111 терапевт Иванова\n"
    "• Пришлите ссылку на расписание с gorzdrav.spb.ru — сразу перейду к выбору врача\n"
    "• Мои подписки — список подписок\n"
    "• Отписаться [номер|все]\n"
    "• Промежуток <номер> <дата или даты> — изменить, например: промежуток 1 05.11\n"
    "• Время <номер> <условие> — фильтр по времени, например: время 1 после 17:00\n"
    "• Карточка — ФИО и дата рождения для записи (удалить: «удалить карточку»)\n"
    "• Согласие — текст согласия на обработку данных; «отозвать согласие» удалит все ваши данные\n"
    "• Автозапись <номер> вкл|выкл — бот сам запишет на самый ранний талон\n"
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


def lpu_label(l, with_addr: bool = False) -> str:
    name = l.get("lpuShortName") or l["lpuFullName"]
    if with_addr:
        parts = [x.strip() for x in (l.get("address") or "").split(",")]
        addr = ", ".join(x for x in parts[2:4] if x)  # 3-й и 4-й элементы
        if addr:
            return f"{name} ({addr})"
    return name


def schedule_link(sub) -> str:
    parts = []
    if sub.get("district_id"):
        parts.append({"district": str(sub["district_id"])})
    parts += [{"lpu": str(sub["lpu_id"])}, {"speciality": str(sub["spec_id"])}]
    frag = urllib.parse.quote(json.dumps(parts, separators=(",", ":")), safe=":,")
    return "https://gorzdrav.spb.ru/service-free-schedule#" + frag


def parse_link(text: str):
    """Достаёт параметры district/lpu/speciality[/doctor] из ссылки на расписание."""
    m = re.search(r"gorzdrav\.spb\.ru/\S*?#(\S+)", text)
    if not m:
        return None
    try:
        arr = json.loads(urllib.parse.unquote(m.group(1)))
    except ValueError:
        return None
    out = {}
    for d in arr if isinstance(arr, list) else []:
        if isinstance(d, dict):
            out.update({k: str(v) for k, v in d.items()})
    return out if out.get("lpu") and out.get("speciality") else None


def start_from_link(uid, params):
    if limit_reached(uid):
        return
    ctx = {}
    try:
        districts = cached("/shared/districts")
        did = params.get("district")
        ctx["district"] = next((d for d in districts if str(d["id"]) == did), None)
        scan = [ctx["district"]] if ctx["district"] else districts
        for d in scan:
            lpu = next((l for l in cached(f"/shared/district/{d['id']}/lpus")
                        if str(l["id"]) == params["lpu"]), None)
            if lpu:
                ctx["district"], ctx["lpu"] = d, lpu
                break
        if "lpu" not in ctx:
            raise LookupError("поликлиника")
        ctx["spec"] = next((x for x in cached(f"/schedule/lpu/{params['lpu']}/specialties", ttl=300)
                            if str(x["id"]) == params["speciality"]), None)
        if not ctx["spec"]:
            raise LookupError("специализация")
        stage = "doctor"
        if params.get("doctor"):
            doc = next((x for x in fetch_doctors(ctx["lpu"]["id"], ctx["spec"]["id"])
                        if str(x["id"]) == params["doctor"]), None)
            if doc:
                ctx["doctor"], stage = doc, "range"
    except LookupError as e:
        send(uid, f"Не нашёл в ссылке: {e}.", kb_main())
        return
    send(uid, f"✔ {lpu_label(ctx['lpu'])}\n✔ {ctx['spec']['name']}")
    STATE[uid] = {"stage": stage, "ctx": ctx, "hints": [], "options": []}
    run_stage(uid)


def items_for(stage, ctx):
    if stage == "district":
        return [{"label": d["name"], "text": d["name"], "val": d} for d in cached("/shared/districts")]
    if stage == "lpu":
        res = cached(f"/shared/district/{ctx['district']['id']}/lpus")
        return [{"label": lpu_label(l, True),
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


def limit_reached(uid) -> bool:
    if len(store.list(uid)) < MAX_SUBS_PER_USER:
        return False
    STATE.pop(uid, None)
    send(uid, f"У вас уже {MAX_SUBS_PER_USER} подписок, это максимум. "
              f"Чтобы добавить новую, отпишитесь от ненужной: «Отписаться».", kb_main())
    return True


def start_wizard(uid, hints):
    if limit_reached(uid):
        return
    STATE[uid] = {"stage": "district", "ctx": {}, "hints": hints, "options": []}
    run_stage(uid)


def run_stage(uid, typed=False):
    st = STATE[uid]
    while True:
        stage = st["stage"]
        if stage == "range":
            send(uid, "Укажите дату или промежуток, например «05.11», «01.11-10.11», «с 05.11» или «до 20.11».\n"
                      "Нажмите «Любая дата», если ограничений нет — тогда сообщу, когда появится запись "
                      "или найдётся более ранняя.", kb_date())
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


TIME_PROMPT = ("Укажите время приёма: «после 17:00», «до 12:00» или «10:00-14:00». "
               "Если время не важно, нажмите «Любое время».")


def wizard_reply(uid, text):
    st = STATE[uid]
    stage = st["stage"]
    if stage.startswith("card_"):
        card_reply(uid, text)
        return
    if stage == "range":
        try:
            st["range"] = parse_range(text)
        except ValueError as e:
            send(uid, f"{e}\nПример: 01.11-10.11")
            return
        st["stage"] = "time"
        send(uid, TIME_PROMPT, kb_time())
        return
    if stage == "time":
        try:
            st["time"] = parse_time_filter(text)
        except ValueError as e:
            send(uid, f"{e}\nПример: после 17:00")
            return
        st["stage"] = "auto"
        send(uid, "Записать вас автоматически?\nЕсли «Да», при появлении талона бот сам запишет вас на самый "
                  "ранний подходящий (нужна карточка пациента), и подписка снимется. "
                  "Если «Нет», пришлю уведомление с кнопкой «Записаться».", kb_yesno())
        return
    if stage == "auto":
        low = norm(text)
        if low.startswith("да"):
            if profiles.get(uid):
                finish_subscription(uid, auto=True)
            else:
                st["stage"], st["card"], st["pending"] = "card_fio", {}, True
                ask_fio(uid, "Для автозаписи нужна карточка пациента. Она хранится на сервере бота, "
                             "удалить можно командой «удалить карточку».\n\n")
        elif low.startswith("нет"):
            finish_subscription(uid, auto=False)
        else:
            send(uid, "Ответьте «Да» или «Нет».")
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


def finish_subscription(uid, auto=False):
    st = STATE.pop(uid)
    c = st["ctx"]
    d_from, d_to = st["range"]
    t_from, t_to = st.get("time", (None, None))
    sub = {
        "id": str(int(time.time() * 1000)),
        "district_id": (c.get("district") or {}).get("id"),
        "lpu_id": c["lpu"]["id"], "lpu_name": lpu_label(c["lpu"]),
        "spec_id": c["spec"]["id"], "spec_name": c["spec"]["name"],
        "doctor_id": c["doctor"]["id"], "doctor_name": short_name(c["doctor"]["name"]),
        "date_from": d_from.isoformat() if d_from else None,
        "date_to": d_to.isoformat() if d_to else None,
        "time_from": t_from, "time_to": t_to,
        "seen": [], "last_nearest": None,
        "auto_book": False, "failed": [], "offers": [],
    }
    existing = store.list(uid)
    if len(existing) >= MAX_SUBS_PER_USER and not any(
            x["lpu_id"] == sub["lpu_id"] and x["doctor_id"] == sub["doctor_id"] for x in existing):
        send(uid, f"Достигнут лимит подписок ({MAX_SUBS_PER_USER}). Удалите ненужные: «Отписаться».", kb_main())
        return
    note = ""
    if auto:
        try:
            find_patient(sub["lpu_id"], profiles.get(uid))
            sub["auto_book"] = True
        except PatientNotFound as e:
            note = (f"\n\n⚠ Автозапись не включена: в этой поликлинике не найден пациент с вашими данными ({e}). "
                    f"Проверьте «Карточку». Уведомления будут приходить.")
        except Exception as e:
            log.warning("patient check failed: %s", e)
            note = "\n\n⚠ Не удалось проверить карточку в поликлинике, автозапись не включена. Повторить: «автозапись N вкл»."
    try:
        info = baseline(sub)
    except Exception as e:
        log.warning("baseline failed: %s", e)
        send(uid, "Не удалось связаться с ГорЗдравом, попробуйте ещё раз позже.", kb_main())
        return
    replaced = store.add(uid, sub)
    send(uid, f"{'Подписка обновлена' if replaced else 'Подписка оформлена'} ✅\n\n{sub_header(sub)}\n"
              f"Промежуток: {range_text(sub)}\n"
              + (f"Время: {time_text(sub)}\n" if has_time(sub) else "") +
              f"Автозапись: {'включена ✅' if sub['auto_book'] else 'нет'}\n\n{info}{note}", kb_main())


# ------------------------------ Карточка пациента ------------------------------
NAME_RE = re.compile(r"[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*")


def cap_name(w: str) -> str:
    return "-".join(p[:1].upper() + p[1:].lower() for p in w.split("-"))


def ask_fio(uid, intro=""):
    send(uid, intro + "Введите ФИО полностью, например: Иванов Иван Иванович. "
                      "Без отчества — только фамилия и имя.", kb_cancel())


def card_text(prof) -> str:
    fio = " ".join(x for x in [prof["last"], prof["first"], prof.get("middle")] if x)
    birth = datetime.fromisoformat(prof["birth"]).strftime("%d.%m.%Y")
    return f"{fio}\nДата рождения: {birth}\nE-mail: {prof.get('email') or 'не указан'}"


def show_card(uid):
    prof = profiles.get(uid)
    if not prof:
        STATE[uid] = {"stage": "card_fio", "card": {}, "pending": False}
        ask_fio(uid)
        return
    send(uid, f"Ваша карточка:\n{card_text(prof)}\n\nЗаполнить заново: «карточка заново». "
              f"Удалить: «удалить карточку».", kb_main())


def card_reply(uid, text):
    st = STATE[uid]
    stage = st["stage"]
    if stage == "card_fio":
        words = text.split()
        if not (2 <= len(words) <= 3) or not all(NAME_RE.fullmatch(w) for w in words):
            send(uid, "Нужны фамилия, имя и отчество через пробел, только буквы.")
            return
        w = [cap_name(x) for x in words]
        st["card"].update(last=w[0], first=w[1], middle=w[2] if len(w) == 3 else "")
        st["stage"] = "card_birth"
        send(uid, "Дата рождения в формате ДД.ММ.ГГГГ:", kb_cancel())
    elif stage == "card_birth":
        try:
            d = datetime.strptime(text.strip(), "%d.%m.%Y").date()
        except ValueError:
            send(uid, "Не понял дату. Формат: ДД.ММ.ГГГГ, например 15.03.1998.")
            return
        if not (date(1900, 1, 1) <= d <= now_msk().date()):
            send(uid, "Дата выглядит неверной, проверьте и введите ещё раз.")
            return
        st["card"]["birth"] = d.isoformat()
        st["stage"] = "card_email"
        send(uid, "E-mail для уведомлений о записи (необязательно). Напишите «-», чтобы пропустить:", kb_cancel())
    elif stage == "card_email":
        t = text.strip()
        if t.lower() in ("-", "нет", "пропустить"):
            email = ""
        elif re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", t):
            email = t
        else:
            send(uid, "Не похоже на e-mail. Введите адрес или «-», чтобы пропустить.")
            return
        st["card"]["email"] = email
        profiles.set(uid, st["card"])
        if st.get("pending"):
            finish_subscription(uid, auto=True)
        else:
            STATE.pop(uid, None)
            send(uid, f"Карточка сохранена ✅\n{card_text(st['card'])}", kb_main())


def cmd_del_card(uid):
    STATE.pop(uid, None)
    had = profiles.delete(uid)
    off = 0
    for s_ in store.list(uid):
        if s_.get("auto_book"):
            store.update(uid, s_["id"], auto_book=False)
            off += 1
    send(uid, ("Карточка удалена." if had else "Карточки не было.") +
         (f" Автозапись отключена у подписок: {off}." if off else ""), kb_main())


def cmd_auto(uid, rest):
    m = re.match(r"(\d+)\s+(вкл\w*|выкл\w*)", norm(rest.strip()))
    subs = store.list(uid)
    if not m or not (1 <= int(m.group(1)) <= len(subs)):
        send(uid, "Формат: автозапись <номер подписки> вкл|выкл, например: автозапись 1 вкл")
        return
    sub = subs[int(m.group(1)) - 1]
    on = m.group(2).startswith("вкл")
    if on:
        prof = profiles.get(uid)
        if not prof:
            send(uid, "Сначала заполните «Карточку».", kb_main())
            return
        try:
            find_patient(sub["lpu_id"], prof)
        except PatientNotFound as e:
            send(uid, f"В поликлинике не найден пациент с вашими данными ({e}). Проверьте «Карточку».", kb_main())
            return
        except Exception as e:
            log.warning("patient check failed: %s", e)
            send(uid, "Не удалось связаться с ГорЗдравом, попробуйте позже.", kb_main())
            return
    store.update(uid, sub["id"], auto_book=on, failed=[])
    send(uid, f"Автозапись {'включена ✅' if on else 'выключена'}: {sub['doctor_name']}", kb_main())


def cmd_book(uid, text):
    """Нажатие кнопки «Записаться дд.мм чч:мм Фамилия»."""
    m = re.match(r"записаться\s+(\d{2}\.\d{2})\s+(\d{2}:\d{2})\s*(.*)$", norm(text))
    if not m:
        send(uid, "Нажмите кнопку «Записаться» под уведомлением о талоне.", kb_main())
        return
    stamp, rest = f"{m.group(1)} {m.group(2)}", m.group(3).strip()
    found = None
    for sub in store.list(uid):
        if rest and not norm(sub["doctor_name"].split()[0]).startswith(rest):
            continue
        for o in sub.get("offers", []):
            if datetime.fromisoformat(o["visitStart"]).strftime("%d.%m %H:%M") == stamp:
                found = (sub, o)
                break
        if found:
            break
    if not found:
        send(uid, "Этого талона уже нет в предложенных. Дождитесь нового уведомления.", kb_main())
        return
    sub, offer = found
    prof = profiles.get(uid)
    if not prof:
        STATE[uid] = {"stage": "card_fio", "card": {}, "pending": False}
        ask_fio(uid, "Для записи нужна карточка пациента. Заполните её, затем нажмите кнопку «Записаться» ещё раз.\n\n")
        return
    try:
        ok, err, code = book(sub, prof, offer)
    except PatientNotFound as e:
        send(uid, f"В поликлинике «{sub['lpu_name']}» не найден пациент с вашими данными ({e}). "
                  f"Проверьте «Карточку».", kb_main())
        return
    if ok:
        store.remove(uid, sub["id"])
        send(uid, booked_text(sub, offer), kb_main())
    else:
        hint = "" if code in NO_ATTACHMENT_CODES else " Возможно, талон уже занят."
        send(uid, f"Не удалось записаться: {err or 'сайт отклонил запись'}.{hint}\n"
                  f"Запись вручную: {schedule_link(sub)}", kb_main())


def book(sub, prof, appt):
    patient_id = find_patient(sub["lpu_id"], prof)
    return create_appointment(sub["lpu_id"], patient_id, prof, appt)


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
    if t.startswith("люб") or t in ("нет", "все", "без ограничений", "-", "–"):
        return None, None
    m = re.fullmatch(r"(?:на\s+)?(\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)", t)
    if m:  # одна конкретная дата
        d = parse_one_date(m.group(1), now_msk().date())
        return d, d
    m = re.fullmatch(r"с\s+(\S+)", t)  # «с 05.11» - только начало
    t = m.group(1) + "-" if m else re.sub(r"^с\s+", "", t)
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


def parse_hhmm(s: str) -> str:
    m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?", s.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        raise ValueError(f"Не понял время «{s.strip()}».")
    return f"{int(m.group(1)):02d}:{m.group(2) or '00'}"


def parse_time_filter(text: str):
    """'после 17:00' / 'до 12' / '10:00-14:00' / 'любое' -> (from|None, to|None)."""
    t = norm(text.strip())
    if t.startswith("люб") or t in ("нет", "все", "без ограничений", "-", "–"):
        return None, None
    m = re.fullmatch(r"(?:после|с)\s+(\S+)", t)
    if m:
        return parse_hhmm(m.group(1)), None
    m = re.fullmatch(r"до\s+(\S+)", t)
    if m:
        return None, parse_hhmm(m.group(1))
    m = re.fullmatch(r"(?:с\s+)?(\S+?)\s*(?:-|–|—|до|по)\s*(\S+)", t)
    if not m:
        raise ValueError("Не понял время.")
    t_from, t_to = parse_hhmm(m.group(1)), parse_hhmm(m.group(2))
    if t_from >= t_to:
        raise ValueError("Начало должно быть раньше конца.")
    return t_from, t_to


def cmd_time(uid, rest):
    m = re.match(r"(\d+)\s+(.+)", rest.strip())
    subs = store.list(uid)
    if not m or not (1 <= int(m.group(1)) <= len(subs)):
        send(uid, "Формат: время <номер подписки> <условие>, например: время 1 после 17:00 (или «любое»)")
        return
    sub = subs[int(m.group(1)) - 1]
    try:
        sub["time_from"], sub["time_to"] = parse_time_filter(m.group(2))
    except ValueError as e:
        send(uid, str(e))
        return
    sub["seen"], sub["last_nearest"] = [], None
    try:
        info = baseline(sub)
    except Exception as e:
        log.warning("baseline failed: %s", e)
        send(uid, "Не удалось связаться с ГорЗдравом, попробуйте позже.")
        return
    store.update(uid, sub["id"], time_from=sub["time_from"], time_to=sub["time_to"],
                 seen=sub["seen"], last_nearest=sub["last_nearest"])
    send(uid, f"Время обновлено: {time_text(sub)}\n{info}", kb_main())


# ------------------------------ Команды ------------------------------
def show_subs(uid):
    subs = store.list(uid)
    if not subs:
        send(uid, "Подписок пока нет. Нажмите «Подписаться».", kb_main())
        return subs
    blocks = [f"{i}. {s['doctor_name']}\n   {s['lpu_name']} · {s['spec_name']}\n   Промежуток: {range_text(s)}"
              + (f"\n   Время: {time_text(s)}" if has_time(s) else "")
              + ("\n   Автозапись: включена" if s.get("auto_book") else "")
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
    link = parse_link(text) if "gorzdrav.spb.ru" in low else None

    if not has_consent(uid):
        consent_gate(uid, low)
        return

    if link:
        start_from_link(uid, link)
    elif low in ("отмена", "меню", "начать", "/start", "start", "помощь"):
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
    elif low in ("карточка", "моя карточка"):
        STATE.pop(uid, None)
        show_card(uid)
    elif low == "карточка заново":
        STATE[uid] = {"stage": "card_fio", "card": {}, "pending": False}
        ask_fio(uid)
    elif low in ("согласие", "политика"):
        send(uid, CONSENT_TEXT)
    elif low == "отозвать согласие":
        cmd_revoke(uid)
    elif low in ("удалить карточку", "удалить данные"):
        cmd_del_card(uid)
    elif word == "время":
        cmd_time(uid, rest)
    elif word == "автозапись":
        cmd_auto(uid, rest)
    elif word == "записаться":
        cmd_book(uid, text)
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
    #session.http.proxies = {"http": "direct", "https": "direct"}
    vk = session.get_api()
    grandfather_existing_users()
    threading.Thread(target=poller, daemon=True).start()
    log.info("Бот запущен (pid %s), опрос каждые %s с", os.getpid(), POLL_INTERVAL)
    recent_ids = deque(maxlen=500)  # защита от повторной доставки одного и того же сообщения

    while True:
        try:
            longpoll = VkLongPoll(session)
            #longpoll.session.proxies = {"http": "direct", "https": "direct"}
            for event in longpoll.listen():
                if event.type != VkEventType.MESSAGE_NEW or not event.to_me or not event.text:
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