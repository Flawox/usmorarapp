from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qsl
import json, os, hmac, hashlib, time
import requests
from bs4 import BeautifulSoup

requests.packages.urllib3.disable_warnings()
URL = "https://studentcrd.usm.md/"

# Сколько всего пар по предмету (как в боте)
TOTALS = [
    (("истор", "istor"), 30), (("географ", "geograf"), 30), (("математ", "matemat"), 30),
    (("английск", "englez", "străin", "strain"), 30), (("физик", "fizic"), 16),
    (("биолог", "biolog"), 15), (("информат", "informat", "tic"), 15), (("хими", "chimi"), 14),
    (("румын", "român", "roman", "alolingv"), 45), (("русск", "rus"), 45),
]

def total(subject):
    s = subject.lower()
    for keys, n in TOTALS:
        if any(k in s for k in keys):
            return n
    return None

def valid_init(init):
    """Если задан BOT_TOKEN — пускаем только запросы из Telegram (проверка подписи initData)."""
    token = os.environ.get("BOT_TOKEN")
    if not token:
        return True
    try:
        d = dict(parse_qsl(init, keep_blank_values=True))
        h = d.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        ok = hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), h)
        return ok and time.time() - int(d["auth_date"]) < 86400
    except Exception:
        return False

def login(idnp):
    s = requests.Session()
    s.verify = False
    soup = BeautifulSoup(s.get(URL, timeout=10).text, "html.parser")
    f = lambda n: (soup.find("input", {"name": n}) or {}).get("value", "")
    if not f("__VIEWSTATE") or not f("__EVENTVALIDATION"):
        raise ValueError("Не удалось получить токены страницы USM")
    r = s.post(URL, timeout=10, data={
        "__VIEWSTATE": f("__VIEWSTATE"), "__VIEWSTATEGENERATOR": f("__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": f("__EVENTVALIDATION"), "txtCodperson": idnp, "btLogin": "Login"})
    res = BeautifulSoup(r.text, "html.parser")
    err = res.find("span", {"id": "lbErr"})
    if err and err.text.strip():
        raise ValueError("Ошибка входа: " + err.text.strip())
    return res

def attendance(soup):
    div = soup.find("div", id="divNoteBysemest")
    if not div or not div.find("table"):
        return []
    items, subj, teacher = {}, "Неизвестный предмет", ""
    for row in div.find("table").find_all("tr"):
        tds = row.find_all("td")
        if not tds:
            continue
        if len(tds) == 1 and tds[0].get("colspan") == "3":
            b = tds[0].find("b")
            if b:
                t = b.text.strip()
                if "semestru" not in t.lower() and "ac" not in t.lower():
                    subj, teacher = t, ""
        elif len(tds) == 2:
            b = tds[1].find("b")
            teacher = b.text.strip() if b else tds[1].text.strip()
        elif len(tds) == 3:
            parts = tds[1].text.strip().split(" ")[0].split(".")
            date = ".".join(parts[:2]) if len(parts) >= 2 else tds[1].text.strip()
            mark = tds[2].text.strip().lower()
            it = items.setdefault((subj, teacher), {"subject": subj, "teacher": teacher, "absences": 0, "logs": []})
            if mark == "a":
                it["absences"] += 1
                it["logs"].append({"d": date, "s": "a"})
            elif mark == "":
                it["logs"].append({"d": date, "s": "p"})
            else:
                it["logs"].append({"d": date, "s": "g", "m": mark})
    out = []
    for it in items.values():
        t = total(it["subject"])
        it["total"] = t
        it["percent"] = round(it["absences"] / t * 100, 1) if t else None
        out.append(it)
    return out

def evaluations(soup):
    div = soup.find("div", id="divEvaluareCur")
    if not div or not div.find("table"):
        return []
    items, subj, teacher = {}, "", ""
    for row in div.find("table").find_all("tr"):
        tds = row.find_all("td")
        if len(tds) < 2:
            continue
        if "w-50" in tds[0].get("class", []) or tds[0].find("b"):
            subj, teacher = tds[0].text.strip(), tds[1].text.strip()
        else:
            name, grade = tds[0].text.strip(), tds[1].text.strip()
            if name and grade:
                items.setdefault((subj, teacher), {"subject": subj, "teacher": teacher, "tests": []})["tests"].append({"name": name, "grade": grade})
    return list(items.values())

class handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            idnp = str(data.get("idnp", "")).strip()
            if not (idnp.isdigit() and len(idnp) == 13):
                return self._send(400, {"error": "IDNP должен состоять ровно из 13 цифр"})
            if not valid_init(data.get("initData", "")):
                return self._send(403, {"error": "Откройте приложение из Telegram"})
            soup = login(idnp)  # IDNP нигде не логируется и не сохраняется
            self._send(200, {"attendance": attendance(soup), "evaluations": evaluations(soup)})
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception:
            self._send(502, {"error": "Сайт USM сейчас недоступен, попробуйте позже"})
