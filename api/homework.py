from datetime import date, timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qsl, urlencode
from urllib.request import Request, urlopen
import hashlib
import hmac
import json
import logging
import os
import re
import time

DAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница")
ODD_WEEK = date(2026, 9, 21)
SCHEDULE_PATH = Path(__file__).resolve().parent.parent / "public" / "schedule.json"
SUBGROUPS = ("1", "2")
FIELDS = "lesson_date,period,subject,lesson_type,subgroup_no,body"


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
        if type(user_id) is int and user_id > 0:
            return str(user_id)
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
    week = "even" if max(0, (day - ODD_WEEK).days) // 7 % 2 else "odd"
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


def supabase(method, filters=None, rows=None):
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not base or not key:
        raise ApiError(503, "В Vercel нужно настроить подключение к Supabase")
    url = f"{base}/rest/v1/homework"
    if filters:
        url += "?" + urlencode(filters)
    headers = {"apikey": key, "Accept": "application/json"}
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    if method == "POST":
        headers["Content-Type"] = "application/json"
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    payload = json.dumps(rows, ensure_ascii=False).encode("utf-8") if rows is not None else None
    with urlopen(Request(url, data=payload, headers=headers, method=method), timeout=7) as response:
        return json.load(response) if method == "GET" else None


def handle_homework(data, is_admin):
    action = data.get("action")
    if action not in ("list", "read", "save", "delete"):
        raise ApiError(400, "Неизвестное действие")
    if action != "list" and not is_admin:
        raise ApiError(403, "Доступно только администратору")

    group, subgroup = data.get("group"), data.get("subgroup")
    if not isinstance(group, str) or group not in schedule():
        raise ApiError(400, "Выберите группу")
    if subgroup not in SUBGROUPS and not (action != "list" and subgroup == "all"):
        raise ApiError(400, "Выберите подгруппу")
    day = read_date(data.get("date"))
    filters = {"group_no": f"eq.{group}", "select": FIELDS}

    if action == "list":
        if day.weekday() != 0:
            raise ApiError(400, "Укажите понедельник нужной недели")
        filters.update(subgroup_no=f"eq.{subgroup}", lesson_date=f"gte.{day}")
        rows = supabase("GET", [*filters.items(), ("lesson_date", f"lt.{day + timedelta(days=7)}")])
        return {"items": rows}

    period = data.get("period")
    if type(period) is not int or period <= 0:
        raise ApiError(400, "Выберите пару")
    subject, lesson_type = data.get("subject"), data.get("lessonType")
    targets = SUBGROUPS if subgroup == "all" else (subgroup,)
    for target in targets:
        lesson = lesson_for(group, target, day, period)
        if not lesson or (lesson["subject"], lesson["type"]) != (subject, lesson_type):
            raise ApiError(400, "Для выбранной подгруппы или даты такой пары нет. Проверьте расписание.")

    filters.update(
        subgroup_no=f"in.({','.join(targets)})",
        lesson_date=f"eq.{day}",
        period=f"eq.{period}",
        subject=f"eq.{subject}",
        lesson_type=f"eq.{lesson_type}",
    )
    if action == "read":
        return {"items": supabase("GET", filters)}
    if action == "delete":
        filters.pop("select")
        supabase("DELETE", filters)
    else:
        body = data.get("body")
        if not isinstance(body, str) or not 1 <= len(body.strip()) <= 3000 or "\x00" in body:
            raise ApiError(400, "Введите от 1 до 3000 символов задания")
        updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rows = [
            {
                "group_no": int(group), "subgroup_no": int(target), "lesson_date": str(day),
                "period": period, "subject": subject, "lesson_type": lesson_type,
                "body": body.strip(), "updated_at": updated_at,
            }
            for target in targets
        ]
        supabase("POST", rows=rows)
    return {}


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
            user_id = telegram_user(data.get("initData"))
            if not user_id:
                raise ApiError(401, "Откройте мини-приложение заново из Telegram")
            is_admin = user_id in os.environ.get("HOMEWORK_ADMIN_IDS", "").replace(",", " ").split()
            result = handle_homework(data, is_admin)
        except ApiError as error:
            self._send(error.status, {"error": str(error), "isAdmin": is_admin})
        except (OSError, ValueError):
            logging.warning("Homework: schedule or database request failed", exc_info=True)
            self._send(503, {"error": "Домашние задания сейчас недоступны. Попробуйте позже.", "isAdmin": is_admin})
        else:
            self._send(200, {**result, "isAdmin": is_admin})
