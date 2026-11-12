from datetime import date, datetime, time as datetime_time, timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from threading import local
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo
import hashlib
import hmac
import json
import logging
import os
import re
import requests
import time

DAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница")
ODD_WEEK = date(2026, 9, 21)
LOCAL_ZONE = ZoneInfo("Europe/Chisinau")
SCHEDULE_PATH = Path(__file__).resolve().parent.parent / "public" / "schedule.json"
SUBGROUPS = ("1", "2")
HTTP_CLIENT = local()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def telegram_user(init_data):
    token = os.environ.get("BOT_TOKEN")
    if not token or not isinstance(init_data, str):
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            return None
        signature = fields.pop("hash")
        if not re.fullmatch(r"[a-f0-9]{64}", signature):
            return None
        check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret = hmac.digest(b"WebAppData", token.encode(), hashlib.sha256)
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return None
        if not -60 <= time.time() - int(fields["auth_date"]) < 86400:
            return None
        user = json.loads(fields["user"])
        user_id = user.get("id") if isinstance(user, dict) else None
        if type(user_id) is int and 0 < user_id < 2**52:
            return user
    except (KeyError, ValueError):
        return None
    return None


@lru_cache(maxsize=1)
def schedule():
    return json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))["schedule"]


def lesson_for(group, subgroup, day, period):
    if day.weekday() >= len(DAYS):
        return None
    slots = schedule()[group][subgroup].get(DAYS[day.weekday()], {})
    slot = slots.get(str(period), {})
    week = "even" if (day - ODD_WEEK).days // 7 % 2 else "odd"
    return slot.get("all") or slot.get(week)


def read_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ApiError(400, "Укажите дату в формате ГГГГ-ММ-ДД")
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise ApiError(400, "Укажите корректную дату") from None
    if not 2000 <= day.year <= 2100:
        raise ApiError(400, "Укажите дату между 2000 и 2100 годами")
    return day


def supabase_rpc(name, params):
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not base or not key:
        raise ApiError(503, "В Vercel нужно настроить подключение к Supabase")
    url = f"{base}/rest/v1/rpc/{name}"
    headers = {"apikey": key, "Accept": "application/json", "Content-Type": "application/json"}
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    payload = json.dumps(params, ensure_ascii=False).encode("utf-8")
    # Each worker thread keeps its own pool of reusable HTTPS connections.
    session = getattr(HTTP_CLIENT, "session", None)
    if session is None:
        session = requests.Session()
        HTTP_CLIENT.session = session
    with session.post(url, data=payload, headers=headers, timeout=7, allow_redirects=False) as response:
        if not 200 <= response.status_code < 300:
            raise ApiError(503, "Домашние задания сейчас недоступны. Попробуйте позже.")
        return response.json()


def author_profile(user):
    username = user.get("username")
    if isinstance(username, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,32}", username):
        name = f"@{username}"
    else:
        name = " ".join(
            user[field].strip() for field in ("first_name", "last_name")
            if isinstance(user.get(field), str) and user[field].strip()
        )
    name = name.replace("\x00", "").strip()[:140] or "Участник"
    photo = user.get("photo_url")
    if isinstance(photo, str) and len(photo) <= 2048 and not re.search(r"[\x00-\x20]", photo):
        try:
            url = urlsplit(photo)
            if url.scheme == "https" and url.hostname and not url.username and not url.password:
                return name, photo
        except ValueError:
            pass
    return name, None


def current_monday():
    today = datetime.now(LOCAL_ZONE).date()
    return today - timedelta(days=today.weekday())


def handle_homework(data, user, is_admin):
    action = data.get("action")
    if action == "save":
        action = "save_admin"
    if action == "delete":
        action = "delete_admin"
    if action not in ("list", "read", "save_admin", "delete_admin", "save_post", "delete_post"):
        raise ApiError(400, "Неизвестное действие")
    if action in ("save_admin", "delete_admin") and not is_admin:
        raise ApiError(403, "Доступно только администратору")
    if action == "save_post" and is_admin:
        raise ApiError(400, "Используйте редактор задания администратора")

    group, subgroup = data.get("group"), data.get("subgroup")
    if not isinstance(group, str) or group not in schedule():
        raise ApiError(400, "Выберите группу")
    if subgroup == "all":
        if not is_admin or action not in ("read", "save_admin", "delete_admin"):
            raise ApiError(400, "Выберите подгруппу")
        targets = SUBGROUPS
        visible_subgroup = data.get("viewSubgroup", "1")
        if visible_subgroup not in SUBGROUPS:
            raise ApiError(400, "Выберите подгруппу")
    elif subgroup in SUBGROUPS:
        targets = (subgroup,)
        visible_subgroup = subgroup
    else:
        raise ApiError(400, "Выберите подгруппу")

    day = read_date(data.get("date"))
    monday = current_monday()
    next_monday = monday + timedelta(days=7)
    lesson_week = day - timedelta(days=day.weekday())
    if lesson_week not in (monday, next_monday):
        raise ApiError(409, "Учебная неделя сменилась. Обновите расписание.")

    if action == "list":
        if day.weekday() != 0:
            raise ApiError(400, "Укажите понедельник нужной недели")
        rows = supabase_rpc("homework_week_v2", {
            "p_group": int(group), "p_subgroup": int(subgroup),
            "p_from": str(day), "p_until": str(day + timedelta(days=7)),
        })
        rollover = datetime.combine(next_monday, datetime_time.min, LOCAL_ZONE)
        return {"items": rows, "rolloverAt": rollover.isoformat()}

    period = data.get("period")
    if type(period) is not int or period <= 0:
        raise ApiError(400, "Выберите пару")
    subject, lesson_type = data.get("subject"), data.get("lessonType")
    for target in targets:
        lesson = lesson_for(group, target, day, period)
        if not lesson or (lesson["subject"], lesson["type"]) != (subject, lesson_type):
            raise ApiError(400, "Для выбранной подгруппы или даты такой пары нет. Проверьте расписание.")

    params = {
        "action": action, "group_no": int(group), "subgroup_no": int(visible_subgroup),
        "targets": list(map(int, targets)), "lesson_date": str(day), "period": period,
        "subject": subject, "lesson_type": lesson_type, "is_admin": is_admin,
        "actor_id": user["id"],
    }
    if action == "read":
        return supabase_rpc("homework_read_v2", {"p": params})

    if action in ("save_admin", "save_post"):
        body = data.get("body")
        if not isinstance(body, str) or not 1 <= len(body.strip()) <= 3000 or "\x00" in body:
            raise ApiError(400, "Введите от 1 до 3000 символов задания")
        params["body"] = body.strip()
        if action == "save_post":
            params["author_name"], params["author_photo"] = author_profile(user)
    if action == "delete_post":
        try:
            params["post_id"] = str(UUID(str(data.get("postId"))))
        except ValueError:
            raise ApiError(400, "Выберите задание для удаления") from None

    result = supabase_rpc("homework_write_v2", {"p": params})
    if result.get("locked"):
        raise ApiError(409, "Администратор уже опубликовал задание для этой пары.")
    if result.get("missing"):
        raise ApiError(404, "Задание уже удалено или недоступно.")
    if result.get("forbidden"):
        raise ApiError(403, "Недостаточно прав для изменения задания.")
    return result


class handler(BaseHTTPRequestHandler):
    def _send(self, status, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        is_admin = False
        try:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 64000:
                    raise ApiError(400, "Некорректный размер запроса")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError
            except ValueError:
                raise ApiError(400, "Некорректный запрос") from None
            if not os.environ.get("BOT_TOKEN"):
                raise ApiError(503, "В Vercel нужно настроить BOT_TOKEN")
            user = telegram_user(data.get("initData"))
            if not user:
                raise ApiError(401, "Откройте мини-приложение заново из Telegram")
            is_admin = str(user["id"]) in os.environ.get("HOMEWORK_ADMIN_IDS", "").replace(",", " ").split()
            result = handle_homework(data, user, is_admin)
        except ApiError as error:
            self._send(error.status, {"error": str(error), "isAdmin": is_admin})
        except (OSError, ValueError):
            logging.warning("Homework: schedule or database request failed", exc_info=True)
            self._send(503, {"error": "Домашние задания сейчас недоступны. Попробуйте позже.", "isAdmin": is_admin})
        else:
            self._send(200, {**result, "isAdmin": is_admin})
