#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import json
import os
import secrets
import threading
import uuid
import webbrowser
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, redirect, request,
                   send_file, session, url_for)

# ---------------------------------------------------------------------------
# الإعدادات
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

# مجلد البيانات: افتراضياً بجانب الملف، ويمكن تغييره عبر متغيّر البيئة DATA_DIR
# (مفيد عند تركيب قرص دائم على منصّات الاستضافة مثل Render).
DATA_DIR = Path(os.environ.get("DATA_DIR") or BASE_DIR)
DATA_FILE = DATA_DIR / "students.json"
LECTURES_DIR = DATA_DIR / "lectures"
SECRET_FILE = DATA_DIR / ".secret_key"

# على الاستضافة السحابية تُضبط PORT تلقائياً، وحينها نستمع على كل العناوين.
_PORT_ENV = os.environ.get("PORT")
DEFAULT_HOST = os.environ.get("HOST") or ("0.0.0.0" if _PORT_ENV else "127.0.0.1")
DEFAULT_PORT = int(_PORT_ENV or 5000)

# بيانات الأدمن: غيّرها عبر متغيّرات البيئة على الاستضافة لأسباب أمنية.
ADMIN_USER = os.environ.get("ADMIN_USER", "root")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "root")

VALID_STATUSES = {"present", "absent", "late"}
DAYS = ["السبت", "الأحد", "الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة"]

MAX_UPLOAD_MB = 50
ALLOWED_EXT = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".txt", ".zip", ".rar", ".png", ".jpg", ".jpeg",
}

app = Flask(__name__)
_lock = threading.Lock()

app.config.update(
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # فعّلها عند التشغيل عبر HTTPS: اضبط COOKIE_SECURE=1 على الاستضافة.
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower()
    in ("1", "true", "yes"),
)

# تأكيد وجود مجلد البيانات (قد يكون قرصاً دائماً على الاستضافة).
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


def _load_secret():
    """مفتاح ثابت للجلسات حتى لا يخرج المستخدمون عند كل إعادة تشغيل."""
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    try:
        if SECRET_FILE.exists():
            key = SECRET_FILE.read_text(encoding="utf-8").strip()
            if key:
                return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        SECRET_FILE.write_text(key, encoding="utf-8")
    except OSError:
        pass
    return key


app.secret_key = _load_secret()


# ---------------------------------------------------------------------------
# قاعدة البيانات (ملف JSON)
# ---------------------------------------------------------------------------
def _empty_db():
    return {
        "students": [],
        "attendance": {},
        "lectures": [],
        "schedule": [],
        "admin": {"username": ADMIN_USER, "password": ADMIN_PASS},
    }


def new_password():
    """رمز بسيط من 4 أرقام."""
    return f"{secrets.randbelow(10000):04d}"


def _same(a, b):
    return secrets.compare_digest(str(a).encode(), str(b).encode())


def _next_exam_no(used):
    n = 1001
    while str(n) in used:
        n += 1
    return str(n)


def _migrate_students(db):
    """يعطي الطلاب القدامى رقماً امتحانياً ورمزاً حتى يستطيعوا الدخول."""
    used = {s.get("exam_no") for s in db["students"] if s.get("exam_no")}
    changed = False
    for s in db["students"]:
        if not s.get("id"):
            s["id"] = uuid.uuid4().hex[:12]
            changed = True
        if not s.get("exam_no"):
            s["exam_no"] = _next_exam_no(used)
            used.add(s["exam_no"])
            changed = True
        if not s.get("password"):
            s["password"] = new_password()
            changed = True
    return changed


def load_db():
    if not DATA_FILE.exists():
        return _empty_db()
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            db = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _empty_db()
    if not isinstance(db, dict):
        return _empty_db()

    db.setdefault("students", [])
    db.setdefault("attendance", {})
    db.setdefault("lectures", [])
    db.setdefault("schedule", [])
    admin = db.setdefault("admin", {})
    admin.setdefault("username", ADMIN_USER)
    admin.setdefault("password", ADMIN_PASS)

    if _migrate_students(db):
        save_db(db)
    return db


def save_db(db):
    """حفظ آمن: نكتب ملف مؤقت ثم نستبدل الأصلي لتجنب تلف البيانات."""
    tmp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def valid_date(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def state_for(db, date):
    """يبني حالة الطلاب لتاريخ معيّن (للأدمن)."""
    day = db["attendance"].get(date, {})
    students = [
        {
            "id": s["id"],
            "name": s["name"],
            "exam_no": s.get("exam_no", ""),
            "password": s.get("password", ""),
            "status": day.get(s["id"], "none"),
        }
        for s in db["students"]
    ]
    return {"date": date, "students": students}


def sorted_schedule(items):
    def key(r):
        day = DAYS.index(r["day"]) if r.get("day") in DAYS else 99
        return (day, r.get("time") or "")
    return sorted(items, key=key)


# ---------------------------------------------------------------------------
# الصلاحيات
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return jsonify({"error": "غير مصرح، سجّل الدخول أولاً"}), 401
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = session.get("user")
        if not user:
            return jsonify({"error": "غير مصرح، سجّل الدخول أولاً"}), 401
        if user.get("role") != "admin":
            return jsonify({"error": "هذه الصلاحية للأدمن فقط"}), 403
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# الصفحات
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    user = session.get("user")
    if not user:
        return redirect(url_for("login_page"))
    return ADMIN_PAGE if user.get("role") == "admin" else STUDENT_PAGE


@app.get("/login")
def login_page():
    if session.get("user"):
        return redirect(url_for("index"))
    return LOGIN_PAGE


@app.get("/static/app.css")
def static_css():
    return Response(BASE_CSS, mimetype="text/css")


@app.get("/static/app.js")
def static_js():
    return Response(APP_JS, mimetype="application/javascript")


@app.get("/healthz")
def healthz():
    """نقطة فحص صحة الخدمة (تستخدمها منصّات الاستضافة)."""
    return jsonify({"ok": True})


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": f"حجم الملف كبير جداً (الحد {MAX_UPLOAD_MB} ميجابايت)"}), 413


# ---------------------------------------------------------------------------
# الدخول والخروج
# ---------------------------------------------------------------------------
@app.post("/api/login")
def api_login():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", "")).strip()

    if not username or not password:
        return jsonify({"error": "الرجاء إدخال الاسم والرمز"}), 400

    with _lock:
        db = load_db()
        admin = db["admin"]
        student = next(
            (s for s in db["students"] if s.get("exam_no") == username), None
        )

    if username == admin["username"] and _same(password, admin["password"]):
        session.clear()
        session["user"] = {"role": "admin", "name": admin["username"]}
        return jsonify({"ok": True, "role": "admin"})

    if student and _same(password, student.get("password", "")):
        session.clear()
        session["user"] = {
            "role": "student",
            "id": student["id"],
            "name": student["name"],
            "exam_no": student.get("exam_no", ""),
        }
        return jsonify({"ok": True, "role": "student"})

    return jsonify({"error": "اسم المستخدم أو الرمز غير صحيح"}), 401


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# الطلاب والحضور (للأدمن)
# ---------------------------------------------------------------------------
@app.get("/api/state")
@admin_required
def api_state():
    date = request.args.get("date", "")
    if not valid_date(date):
        return jsonify({"error": "صيغة التاريخ غير صحيحة"}), 400
    with _lock:
        db = load_db()
        data = state_for(db, date)
    return jsonify(data)


@app.post("/api/students")
@admin_required
def api_add_student():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    exam_no = str(payload.get("exam_no", "")).strip()

    if not name:
        return jsonify({"error": "الرجاء إدخال اسم الطالب"}), 400
    if len(name) > 100:
        return jsonify({"error": "الاسم طويل جداً (الحد 100 حرف)"}), 400
    if not exam_no:
        return jsonify({"error": "الرجاء إدخال الرقم الامتحاني"}), 400
    if not exam_no.isdigit() or len(exam_no) > 20:
        return jsonify({"error": "الرقم الامتحاني يجب أن يكون أرقاماً فقط"}), 400

    with _lock:
        db = load_db()
        if any(s["name"].casefold() == name.casefold() for s in db["students"]):
            return jsonify({"error": "هذا الاسم موجود مسبقاً"}), 409
        if any(s.get("exam_no") == exam_no for s in db["students"]):
            return jsonify({"error": "هذا الرقم الامتحاني مستخدم مسبقاً"}), 409

        student = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "exam_no": exam_no,
            "password": new_password(),
            "created": datetime.now().isoformat(timespec="seconds"),
        }
        db["students"].append(student)
        save_db(db)

    return jsonify({"ok": True, "student": student}), 201


@app.post("/api/students/<sid>/password")
@admin_required
def api_reset_password(sid):
    with _lock:
        db = load_db()
        student = next((s for s in db["students"] if s["id"] == sid), None)
        if student is None:
            return jsonify({"error": "الطالب غير موجود"}), 404
        student["password"] = new_password()
        save_db(db)
    return jsonify({"ok": True, "student": student})


@app.delete("/api/students/<sid>")
@admin_required
def api_delete_student(sid):
    with _lock:
        db = load_db()
        before = len(db["students"])
        db["students"] = [s for s in db["students"] if s["id"] != sid]
        if len(db["students"]) == before:
            return jsonify({"error": "الطالب غير موجود"}), 404
        for day in db["attendance"].values():
            day.pop(sid, None)
        save_db(db)
    return jsonify({"ok": True})


@app.post("/api/attendance")
@admin_required
def api_save_attendance():
    payload = request.get_json(silent=True) or {}
    date = payload.get("date", "")
    records = payload.get("records", {})

    if not valid_date(date):
        return jsonify({"error": "صيغة التاريخ غير صحيحة"}), 400
    if not isinstance(records, dict):
        return jsonify({"error": "بيانات الحضور غير صحيحة"}), 400

    with _lock:
        db = load_db()
        ids = {s["id"] for s in db["students"]}
        day = db["attendance"].setdefault(date, {})
        for sid, status in records.items():
            if sid not in ids:
                continue
            if status == "none":
                day.pop(sid, None)
            elif status in VALID_STATUSES:
                day[sid] = status
        if not day:
            db["attendance"].pop(date, None)
        save_db(db)
        data = state_for(db, date)

    return jsonify({"ok": True, **data})


# ---------------------------------------------------------------------------
# المحاضرات
# ---------------------------------------------------------------------------
@app.get("/api/lectures")
@login_required
def api_lectures():
    with _lock:
        db = load_db()
    return jsonify({"lectures": db["lectures"]})


@app.post("/api/lectures")
@admin_required
def api_add_lecture():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "الرجاء اختيار ملف"}), 400

    original = Path(upload.filename).name
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_EXT:
        return jsonify({"error": f"نوع الملف غير مسموح ({ext or 'بدون امتداد'})"}), 400

    title = str(request.form.get("title", "")).strip() or Path(original).stem
    lid = uuid.uuid4().hex[:12]
    LECTURES_DIR.mkdir(exist_ok=True)
    stored = f"{lid}{ext}"
    upload.save(str(LECTURES_DIR / stored))
    size = (LECTURES_DIR / stored).stat().st_size

    entry = {
        "id": lid,
        "title": title[:120],
        "filename": original,
        "stored": stored,
        "size": size,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    with _lock:
        db = load_db()
        db["lectures"].append(entry)
        save_db(db)

    return jsonify({"ok": True, "lecture": entry}), 201


@app.delete("/api/lectures/<lid>")
@admin_required
def api_delete_lecture(lid):
    with _lock:
        db = load_db()
        entry = next((l for l in db["lectures"] if l["id"] == lid), None)
        if entry is None:
            return jsonify({"error": "المحاضرة غير موجودة"}), 404
        db["lectures"] = [l for l in db["lectures"] if l["id"] != lid]
        save_db(db)
    try:
        (LECTURES_DIR / entry["stored"]).unlink(missing_ok=True)
    except OSError:
        pass
    return jsonify({"ok": True})


@app.get("/download/<lid>")
@login_required
def download_lecture(lid):
    with _lock:
        db = load_db()
    entry = next((l for l in db["lectures"] if l["id"] == lid), None)
    if entry is None:
        abort(404)
    path = LECTURES_DIR / entry["stored"]
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=entry["filename"])


# ---------------------------------------------------------------------------
# الجدول
# ---------------------------------------------------------------------------
@app.get("/api/schedule")
@login_required
def api_schedule():
    with _lock:
        db = load_db()
    return jsonify({"schedule": sorted_schedule(db["schedule"])})


@app.post("/api/schedule")
@admin_required
def api_add_schedule():
    payload = request.get_json(silent=True) or {}
    day = str(payload.get("day", "")).strip()
    time = str(payload.get("time", "")).strip()
    subject = str(payload.get("subject", "")).strip()
    room = str(payload.get("room", "")).strip()

    if day not in DAYS:
        return jsonify({"error": "الرجاء اختيار اليوم"}), 400
    if not subject:
        return jsonify({"error": "الرجاء إدخال اسم المادة"}), 400

    entry = {
        "id": uuid.uuid4().hex[:12],
        "day": day,
        "time": time[:20],
        "subject": subject[:120],
        "room": room[:60],
    }
    with _lock:
        db = load_db()
        db["schedule"].append(entry)
        save_db(db)
    return jsonify({"ok": True, "item": entry}), 201


@app.delete("/api/schedule/<rid>")
@admin_required
def api_delete_schedule(rid):
    with _lock:
        db = load_db()
        before = len(db["schedule"])
        db["schedule"] = [r for r in db["schedule"] if r["id"] != rid]
        if len(db["schedule"]) == before:
            return jsonify({"error": "الحصة غير موجودة"}), 404
        save_db(db)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# واجهة الطالب
# ---------------------------------------------------------------------------
@app.get("/api/me/attendance")
@login_required
def api_me_attendance():
    user = session.get("user") or {}
    if user.get("role") != "student":
        return jsonify({"error": "هذا القسم خاص بالطلاب"}), 403

    with _lock:
        db = load_db()
        me = next((s for s in db["students"] if s["id"] == user.get("id")), None)
        if me is None:
            session.clear()
            return jsonify({"error": "الحساب غير موجود"}), 401
        info = {"name": me["name"], "exam_no": me.get("exam_no", "")}
        rows = [
            {"date": date, "status": day[me["id"]]}
            for date, day in sorted(db["attendance"].items(), reverse=True)
            if me["id"] in day
        ]

    counts = {"present": 0, "absent": 0, "late": 0}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return jsonify({"name": info["name"], "exam_no": info["exam_no"],
                    "rows": rows, "counts": counts})


# ---------------------------------------------------------------------------
# CSS المشترك
# ---------------------------------------------------------------------------
BASE_CSS = r"""
  :root{
    --bg:#eef2f7; --surface:#ffffff; --ink:#0f172a; --muted:#64748b;
    --line:#e2e8f0; --line2:#f1f5f9;
    --brand:#4f46e5; --brand2:#7c3aed; --brand-soft:#eef2ff;
    --present:#16a34a; --present-soft:#dcfce7;
    --absent:#dc2626; --absent-soft:#fee2e2;
    --late:#d97706; --late-soft:#fef3c7;
    --radius:16px;
    --shadow-sm:0 1px 2px rgba(15,23,42,.06);
    --shadow:0 14px 34px rgba(15,23,42,.12);
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:"Segoe UI",Tahoma,system-ui,-apple-system,"Noto Sans Arabic",sans-serif;
    -webkit-font-smoothing:antialiased}
  a{color:var(--brand);text-decoration:none}
  h1,h2,h3{margin:0}
  .ic{width:18px;height:18px;flex:none}
  .muted{color:var(--muted);font-size:13px}

  .btn{display:inline-flex;align-items:center;gap:8px;justify-content:center;
    font-family:inherit;font-size:14px;font-weight:700;cursor:pointer;
    border:1px solid transparent;border-radius:12px;padding:11px 16px;
    background:var(--brand);color:#fff;transition:.18s;white-space:nowrap}
  .btn:hover{filter:brightness(1.07);transform:translateY(-1px)}
  .btn.primary{background:linear-gradient(135deg,var(--brand),var(--brand2));
    box-shadow:0 8px 20px rgba(79,70,229,.28)}
  .btn.ghost{background:#fff;color:var(--ink);border-color:var(--line)}
  .btn.ghost:hover{background:var(--line2)}
  .btn.danger{background:#fff;color:var(--absent);border-color:#fecaca}
  .btn.danger:hover{background:var(--absent-soft)}
  .btn.sm{padding:8px 12px;font-size:13px;border-radius:10px}
  .btn.icon{padding:9px;border-radius:10px}
  .btn:disabled{opacity:.55;cursor:not-allowed;transform:none}

  .field{display:flex;flex-direction:column;gap:6px;min-width:0}
  .field label{font-size:12.5px;font-weight:700;color:var(--muted)}
  .input{width:100%;padding:11px 13px;border:1px solid var(--line);border-radius:12px;
    font-size:14.5px;font-family:inherit;background:#fff;color:var(--ink);transition:.15s}
  .input:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 4px var(--brand-soft)}
  .input::placeholder{color:#94a3b8}
  select.input{cursor:pointer}

  .app{display:flex;min-height:100vh}
  .sidebar{width:258px;flex:none;background:linear-gradient(180deg,#111827,#1e293b);
    color:#e2e8f0;display:flex;flex-direction:column;padding:18px 14px;gap:16px;
    position:sticky;top:0;height:100vh}
  .brand{display:flex;align-items:center;gap:11px;padding:6px 8px 14px;
    border-bottom:1px solid rgba(255,255,255,.08)}
  .brand .logo{width:40px;height:40px;border-radius:12px;display:grid;place-items:center;
    background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff;flex:none}
  .brand b{display:block;font-size:15px;color:#fff}
  .brand small{color:#94a3b8;font-size:12px}
  .nav{display:flex;flex-direction:column;gap:6px;flex:1}
  .nav-item{display:flex;align-items:center;gap:11px;padding:11px 13px;border-radius:12px;
    color:#cbd5e1;font-size:14.5px;font-weight:600;cursor:pointer;transition:.15s;
    border:0;background:none;font-family:inherit;text-align:right;width:100%}
  .nav-item:hover{background:rgba(255,255,255,.07);color:#fff}
  .nav-item.on{background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff;
    box-shadow:0 8px 20px rgba(79,70,229,.35)}
  .side-foot{border-top:1px solid rgba(255,255,255,.08);padding-top:14px;
    display:flex;flex-direction:column;gap:10px}
  .side-user{display:flex;align-items:center;gap:10px;padding:4px 8px}
  .side-user .avatar{width:36px;height:36px;border-radius:50%;background:rgba(255,255,255,.12);
    display:grid;place-items:center;flex:none}
  .side-user b{display:block;font-size:14px;color:#fff}
  .side-user small{color:#94a3b8;font-size:12px}

  .main{flex:1;min-width:0;display:flex;flex-direction:column}
  .topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;
    padding:18px 26px;background:#fff;border-bottom:1px solid var(--line);
    position:sticky;top:0;z-index:20}
  .topbar h2{font-size:19px}
  .chip{display:inline-flex;align-items:center;gap:7px;background:var(--brand-soft);
    color:var(--brand);padding:7px 12px;border-radius:999px;font-size:13px;font-weight:700}
  .content{padding:24px 26px 130px;max-width:1180px;width:100%}

  .card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
    padding:18px;box-shadow:var(--shadow-sm);margin-bottom:18px}
  .card-title{font-size:15px;margin-bottom:14px;display:flex;align-items:center;gap:8px}
  .row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
  .grow{flex:1 1 200px}

  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
    gap:14px;margin-bottom:18px}
  .stat{background:#fff;border:1px solid var(--line);border-radius:var(--radius);
    padding:16px;box-shadow:var(--shadow-sm);display:flex;align-items:center;gap:12px}
  .stat .statchip{width:44px;height:44px;border-radius:12px;display:grid;place-items:center;flex:none}
  .stat .statchip .ic{width:22px;height:22px}
  .stat b{font-size:22px;display:block;line-height:1.1}
  .stat span{font-size:12.5px;color:var(--muted)}

  .table-card{padding:0;overflow-x:auto}
  table{width:100%;border-collapse:collapse;min-width:620px}
  th,td{padding:13px 14px;border-bottom:1px solid var(--line);text-align:right;font-size:14px}
  th{font-size:12.5px;color:var(--muted);font-weight:700;background:var(--line2)}
  tbody tr{transition:background .15s}
  tbody tr:hover{background:#fafbff}
  tr:last-child td{border-bottom:none}
  tbody tr.pending{background:#fffbeb}
  tbody tr.pending:hover{background:#fef9e0}
  .code{font-family:Consolas,monospace;background:var(--line2);border:1px solid var(--line);
    border-radius:8px;padding:3px 9px;font-size:13px;font-weight:700}

  .seg{display:inline-flex;border:1px solid var(--line);border-radius:11px;overflow:hidden;background:#fff}
  .seg button{background:#fff;color:var(--muted);border:0;border-inline-start:1px solid var(--line);
    padding:8px 13px;font-size:13px;font-weight:700;font-family:inherit;cursor:pointer;transition:.15s}
  .seg button:first-child{border-inline-start:0}
  .seg button:hover{background:var(--line2)}
  .seg button.on[data-s=present]{background:var(--present);color:#fff}
  .seg button.on[data-s=absent]{background:var(--absent);color:#fff}
  .seg button.on[data-s=late]{background:var(--late);color:#fff}

  .badge{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;
    border-radius:999px;font-size:12.5px;font-weight:800}
  .badge.present{background:var(--present-soft);color:#15803d}
  .badge.absent{background:var(--absent-soft);color:#b91c1c}
  .badge.late{background:var(--late-soft);color:#b45309}

  .savebar{position:fixed;bottom:22px;inset-inline-start:50%;
    transform:translateX(50%) translateY(160%);
    background:#0f172a;color:#fff;border-radius:16px;padding:12px 16px;
    display:flex;align-items:center;gap:16px;box-shadow:0 18px 40px rgba(15,23,42,.35);
    transition:transform .25s ease;z-index:60;min-width:min(92vw,560px);
    justify-content:space-between}
  .savebar.show{transform:translateX(50%) translateY(0)}
  .savebar-info{display:flex;align-items:center;gap:9px;font-size:14px;font-weight:600}
  .savebar .dot{width:9px;height:9px;border-radius:50%;background:#fbbf24;flex:none;
    box-shadow:0 0 0 4px rgba(251,191,36,.25)}
  .savebar-actions{display:flex;gap:8px}
  .savebar .btn.save{background:linear-gradient(135deg,#22c55e,#16a34a);box-shadow:none}

  .empty{text-align:center;color:var(--muted);padding:36px 14px;font-size:14px}
  .empty svg{width:34px;height:34px;margin:0 auto 10px;display:block;color:#cbd5e1}

  .toast{position:fixed;top:20px;inset-inline-start:50%;
    transform:translateX(50%) translateY(-180%);
    background:#0f172a;color:#fff;padding:12px 18px;border-radius:12px;
    font-size:14px;font-weight:600;box-shadow:var(--shadow);
    transition:transform .25s ease;z-index:80;max-width:90vw}
  .toast.show{transform:translateX(50%) translateY(0)}
  .toast.err{background:#b91c1c}
  .toast.ok{background:#15803d}

  .auth{min-height:100vh;display:grid;place-items:center;padding:22px;
    background:radial-gradient(1200px 600px at 85% -15%,#6366f1 0%,transparent 55%),
      radial-gradient(900px 520px at 0% 115%,#a855f7 0%,transparent 52%),
      linear-gradient(135deg,#0f172a,#1e293b)}
  .auth-card{width:100%;max-width:420px;background:rgba(255,255,255,.985);
    border-radius:22px;padding:32px 28px;box-shadow:0 30px 70px rgba(2,6,23,.5)}
  .auth-logo{width:62px;height:62px;border-radius:18px;margin:0 auto 16px;
    display:grid;place-items:center;background:linear-gradient(135deg,var(--brand),var(--brand2));
    color:#fff;box-shadow:0 12px 28px rgba(79,70,229,.4)}
  .auth-logo .ic{width:32px;height:32px}
  .auth h1{font-size:21px;text-align:center}
  .auth .sub{text-align:center;color:var(--muted);font-size:13.5px;margin:6px 0 22px}
  .pass-wrap{position:relative}
  .pass-wrap .input{padding-inline-end:46px}
  .pass-wrap .toggle{position:absolute;inset-inline-end:6px;top:50%;transform:translateY(-50%);
    background:none;border:0;padding:8px;cursor:pointer;color:var(--muted);display:grid;place-items:center}
  .auth-note{margin-top:20px;text-align:center;font-size:12.5px;color:var(--muted);
    border-top:1px dashed var(--line);padding-top:16px}
  .auth-note .code{margin-inline:3px}

  .tabpane{display:none}
  .tabpane.on{display:block}

  @media (max-width:880px){
    .app{flex-direction:column}
    .sidebar{width:100%;height:auto;position:static;padding:12px}
    .nav{flex-direction:row;overflow-x:auto;gap:8px}
    .nav-item{width:auto;white-space:nowrap}
    .side-foot{flex-direction:row;align-items:center;justify-content:space-between;padding-top:10px}
    .side-user{display:none}
    .content{padding:16px 14px 140px}
    .topbar{padding:14px 16px}
    .topbar h2{font-size:17px}
  }
"""


# ---------------------------------------------------------------------------
# JavaScript المشترك
# ---------------------------------------------------------------------------
APP_JS = r"""
const ICONS = {
  cap:'<path d="M22 10L12 5 2 10l10 5 10-5z"/><path d="M6 12v5c0 1.5 2.7 3 6 3s6-1.5 6-3v-5"/>',
  users:'<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  user:'<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  userCheck:'<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><polyline points="17 11 19 13 23 9"/>',
  book:'<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>',
  calendar:'<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>',
  logout:'<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
  plus:'<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  trash:'<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/>',
  refresh:'<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
  save:'<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>',
  download:'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
  upload:'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
  search:'<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
  eye:'<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
  eyeOff:'<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>',
  check:'<polyline points="20 6 9 17 4 12"/>',
  checkCircle:'<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
  xCircle:'<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>',
  clock:'<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
  minus:'<circle cx="12" cy="12" r="10"/><line x1="8" y1="12" x2="16" y2="12"/>',
  inbox:'<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>'
};

function ic(name){
  return '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    (ICONS[name] || "") + '</svg>';
}

const $ = (id) => document.getElementById(id);

function toast(msg, type){
  const t = $("toast");
  if(!t) return;
  t.textContent = msg;
  t.className = "toast show" + (type === "err" ? " err" : type === "ok" ? " ok" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.className = "toast"; }, 2600);
}

async function api(url, opts){
  const res = await fetch(url, opts);
  if(res.status === 401){ location.href = "/login"; throw new Error("انتهت الجلسة"); }
  let body = {};
  try { body = await res.json(); } catch(e){}
  if(!res.ok) throw new Error(body.error || "حدث خطأ غير متوقع");
  return body;
}

function fmtSize(b){
  if(b === null || b === undefined) return "";
  if(b < 1024) return b + " بايت";
  if(b < 1048576) return (b/1024).toFixed(1) + " كيلوبايت";
  return (b/1048576).toFixed(1) + " ميجابايت";
}

function wireTabs(){
  const tabs = Array.from(document.querySelectorAll("[data-tab]"));
  tabs.forEach(btn => btn.addEventListener("click", () => {
    const name = btn.dataset.tab;
    tabs.forEach(b => b.classList.toggle("on", b === btn));
    document.querySelectorAll("[data-pane]").forEach(p =>
      p.classList.toggle("on", p.dataset.pane === name));
    if(btn.dataset.title){
      const el = $("pageTitle");
      if(el) el.textContent = btn.dataset.title;
    }
  }));
}

function doLogout(){
  fetch("/api/logout", {method:"POST"}).finally(() => { location.href = "/login"; });
}

function todayISO(){
  const d = new Date();
  const off = d.getTimezoneOffset();
  return new Date(d.getTime() - off*60000).toISOString().slice(0,10);
}

const STATUS_LABEL = { present:"حاضر", absent:"غائب", late:"متأخر", none:"--" };
"""


# ---------------------------------------------------------------------------
# صفحة تسجيل الدخول
# ---------------------------------------------------------------------------
LOGIN_PAGE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>تسجيل الدخول</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%234f46e5'%3E%3Cpath d='M22 10L12 5 2 10l10 5 10-5z'/%3E%3Cpath d='M6 12v5c0 1.5 2.7 3 6 3s6-1.5 6-3v-5' fill='none' stroke='%234f46e5' stroke-width='2'/%3E%3C/svg%3E">
<link rel="stylesheet" href="/static/app.css">
</head>
<body class="auth">
  <div class="auth-card">
    <div class="auth-logo" id="logo"></div>
    <h1>نظام الحضور والمحاضرات</h1>
    <p class="sub">سجّل الدخول للوصول إلى لوحتك</p>

    <div class="field">
      <label for="u">اسم المستخدم</label>
      <input id="u" class="input" type="text" placeholder="الرقم الامتحاني أو root"
             autocomplete="username">
    </div>

    <div style="height:14px"></div>

    <div class="field">
      <label for="p">الرمز</label>
      <div class="pass-wrap">
        <input id="p" class="input" type="password" placeholder="••••" autocomplete="current-password">
        <button class="toggle" id="toggle" type="button" title="إظهار/إخفاء"></button>
      </div>
    </div>

    <div style="height:20px"></div>

    <button class="btn primary" id="btn" style="width:100%;padding:13px">دخول</button>
  </div>

<div class="toast" id="toast"></div>
<script src="/static/app.js"></script>
<script>
$("logo").innerHTML = ic("cap");
$("toggle").innerHTML = ic("eye");

function setVisible(v){
  const p = $("p");
  p.type = v ? "text" : "password";
  $("toggle").innerHTML = ic(v ? "eyeOff" : "eye");
}

async function login(){
  const username = $("u").value.trim();
  const password = $("p").value.trim();
  if(!username || !password){ toast("أدخل الاسم والرمز", "err"); return; }
  const btn = $("btn");
  btn.disabled = true;
  try{
    await api("/api/login", {
      method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ username, password })
    });
    location.href = "/";
  }catch(e){
    toast(e.message, "err");
    btn.disabled = false;
  }
}

$("toggle").addEventListener("click", () => setVisible($("p").type === "password"));
$("btn").addEventListener("click", login);
$("u").addEventListener("keydown", e => { if(e.key === "Enter") $("p").focus(); });
$("p").addEventListener("keydown", e => { if(e.key === "Enter") login(); });
$("u").focus();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# صفحة الأدمن
# ---------------------------------------------------------------------------
DAY_OPTIONS = "".join(f'<option value="{d}">{d}</option>' for d in DAYS)

ADMIN_PAGE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>لوحة الأدمن</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%234f46e5'%3E%3Cpath d='M22 10L12 5 2 10l10 5 10-5z'/%3E%3Cpath d='M6 12v5c0 1.5 2.7 3 6 3s6-1.5 6-3v-5' fill='none' stroke='%234f46e5' stroke-width='2'/%3E%3C/svg%3E">
<link rel="stylesheet" href="/static/app.css">
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand">
      <span class="logo" id="brandLogo"></span>
      <div><b>نظام الحضور</b><small>لوحة الأدمن</small></div>
    </div>

    <nav class="nav">
      <button class="nav-item on" data-tab="students" data-title="الطلاب والحضور">
        <span id="nav1"></span><span>الطلاب والحضور</span>
      </button>
      <button class="nav-item" data-tab="lectures" data-title="المحاضرات">
        <span id="nav2"></span><span>المحاضرات</span>
      </button>
      <button class="nav-item" data-tab="schedule" data-title="الجدول الدراسي">
        <span id="nav3"></span><span>الجدول الدراسي</span>
      </button>
    </nav>

    <div class="side-foot">
      <div class="side-user">
        <span class="avatar" id="ava"></span>
        <div><b>root</b><small>مدير النظام</small></div>
      </div>
      <button class="btn ghost sm" id="logout"><span id="out1"></span>خروج</button>
    </div>
  </aside>

  <main class="main">
    <header class="topbar">
      <h2 id="pageTitle">الطلاب والحضور</h2>
      <span class="chip" id="dateChip"></span>
    </header>

    <div class="content">

      <!-- ================= الطلاب والحضور ================= -->
      <section class="tabpane on" data-pane="students">
        <div class="card">
          <h3 class="card-title"><span id="t1"></span>إضافة طالب جديد</h3>
          <div class="row">
            <div class="field grow">
              <label for="name">اسم الطالب</label>
              <input id="name" class="input" type="text" placeholder="اكتب اسم الطالب"
                     maxlength="100" autocomplete="off">
            </div>
            <div class="field" style="flex:0 1 210px">
              <label for="exam">الرقم الامتحاني (اسم المستخدم)</label>
              <input id="exam" class="input" type="text" placeholder="مثال: 1005"
                     maxlength="20" autocomplete="off">
            </div>
            <div>
              <button class="btn primary" id="addBtn"><span id="t2"></span>إضافة الطالب</button>
            </div>
          </div>
          <p class="muted" style="margin:12px 0 0">سيتم توليد رمز دخول تلقائياً لكل طالب ويظهر في الجدول.</p>
        </div>

        <div class="card">
          <div class="row">
            <div class="field" style="flex:0 1 220px">
              <label for="date">تاريخ الحضور</label>
              <input id="date" class="input" type="date">
            </div>
            <div class="field grow">
              <label for="search">بحث</label>
              <input id="search" class="input" type="text" placeholder="الاسم أو الرقم الامتحاني..."
                     autocomplete="off">
            </div>
            <div>
              <button class="btn ghost" id="allPresent"><span id="t3"></span>تحديد الكل حاضر</button>
            </div>
          </div>
        </div>

        <div class="stats">
          <div class="stat">
            <span class="statchip" id="sc1"></span>
            <div><b id="cTotal">0</b><span>إجمالي الطلاب</span></div>
          </div>
          <div class="stat">
            <span class="statchip" id="sc2"></span>
            <div><b id="cPresent">0</b><span>حاضر</span></div>
          </div>
          <div class="stat">
            <span class="statchip" id="sc3"></span>
            <div><b id="cAbsent">0</b><span>غائب</span></div>
          </div>
          <div class="stat">
            <span class="statchip" id="sc4"></span>
            <div><b id="cLate">0</b><span>متأخر</span></div>
          </div>
          <div class="stat">
            <span class="statchip" id="sc5"></span>
            <div><b id="cNone">0</b><span>لم يُسجّل</span></div>
          </div>
        </div>

        <div class="card table-card">
          <table>
            <thead>
              <tr>
                <th style="width:46px">#</th>
                <th>الاسم</th>
                <th style="width:120px">الرقم الامتحاني</th>
                <th style="width:90px">الرمز</th>
                <th style="width:235px">الحضور</th>
                <th style="width:110px">إجراءات</th>
              </tr>
            </thead>
            <tbody id="tbody"></tbody>
          </table>
          <div class="empty" id="empty" style="display:none"></div>
        </div>
        <p class="muted" id="hint"></p>
      </section>

      <!-- ================= المحاضرات ================= -->
      <section class="tabpane" data-pane="lectures">
        <div class="card">
          <h3 class="card-title"><span id="l1"></span>رفع محاضرة</h3>
          <div class="row">
            <div class="field grow">
              <label for="lecTitle">عنوان المحاضرة (اختياري)</label>
              <input id="lecTitle" class="input" type="text" placeholder="مثال: المحاضرة الأولى"
                     maxlength="120">
            </div>
            <div class="field grow">
              <label for="lecFile">الملف</label>
              <input id="lecFile" class="input" type="file">
            </div>
            <div>
              <button class="btn primary" id="lecBtn"><span id="l2"></span>رفع</button>
            </div>
          </div>
          <p class="muted" style="margin:12px 0 0">
            الأنواع المسموحة: PDF، Word، PowerPoint، Excel، صور، zip. الحد 50 ميجابايت.
          </p>
        </div>

        <div class="card table-card">
          <table>
            <thead>
              <tr>
                <th>العنوان</th>
                <th>الملف</th>
                <th style="width:120px">الحجم</th>
                <th style="width:120px">إجراءات</th>
              </tr>
            </thead>
            <tbody id="lecBody"></tbody>
          </table>
          <div class="empty" id="lecEmpty" style="display:none"></div>
        </div>
      </section>

      <!-- ================= الجدول ================= -->
      <section class="tabpane" data-pane="schedule">
        <div class="card">
          <h3 class="card-title"><span id="s1"></span>إضافة حصة</h3>
          <div class="row">
            <div class="field" style="flex:0 1 150px">
              <label for="sDay">اليوم</label>
              <select id="sDay" class="input">__DAY_OPTIONS__</select>
            </div>
            <div class="field" style="flex:0 1 140px">
              <label for="sTime">الوقت</label>
              <input id="sTime" class="input" type="time">
            </div>
            <div class="field grow">
              <label for="sSubject">المادة</label>
              <input id="sSubject" class="input" type="text" placeholder="مثال: الرياضيات" maxlength="120">
            </div>
            <div class="field grow">
              <label for="sRoom">القاعة / الملاحظات</label>
              <input id="sRoom" class="input" type="text" placeholder="اختياري" maxlength="60">
            </div>
            <div>
              <button class="btn primary" id="sBtn"><span id="s2"></span>إضافة</button>
            </div>
          </div>
        </div>

        <div class="card table-card">
          <table>
            <thead>
              <tr>
                <th style="width:120px">اليوم</th>
                <th style="width:120px">الوقت</th>
                <th>المادة</th>
                <th style="width:190px">القاعة</th>
                <th style="width:90px">حذف</th>
              </tr>
            </thead>
            <tbody id="sBody"></tbody>
          </table>
          <div class="empty" id="sEmpty" style="display:none"></div>
        </div>
      </section>
    </div>
  </main>
</div>

<div class="savebar" id="savebar">
  <span class="savebar-info"><span class="dot"></span><span id="saveInfo">لا توجد تغييرات</span></span>
  <span class="savebar-actions">
    <button class="btn ghost sm" id="discardBtn">إلغاء</button>
    <button class="btn save" id="saveBtn"></button>
  </span>
</div>

<div class="toast" id="toast"></div>
<script src="/static/app.js"></script>
<script>
/* ------- أيقونات ثابتة ------- */
$("brandLogo").innerHTML = ic("cap");
$("nav1").innerHTML = ic("users");
$("nav2").innerHTML = ic("book");
$("nav3").innerHTML = ic("calendar");
$("ava").innerHTML = ic("user");
$("out1").innerHTML = ic("logout");
$("t1").innerHTML = ic("plus");
$("t2").innerHTML = ic("plus");
$("t3").innerHTML = ic("checkCircle");
$("l1").innerHTML = ic("upload");
$("l2").innerHTML = ic("upload");
$("s1").innerHTML = ic("plus");
$("s2").innerHTML = ic("plus");
$("saveBtn").innerHTML = ic("save") + "حفظ التغييرات";
$("empty").innerHTML = ic("inbox") + "لا يوجد طلاب بعد. أضف طالباً من الأعلى.";
$("lecEmpty").innerHTML = ic("inbox") + "لا توجد محاضرات بعد.";
$("sEmpty").innerHTML = ic("inbox") + "لا توجد حصص بعد.";
$("sc1").innerHTML = ic("users"); $("sc1").style.background = "var(--brand-soft)"; $("sc1").style.color = "var(--brand)";
$("sc2").innerHTML = ic("checkCircle"); $("sc2").style.background = "var(--present-soft)"; $("sc2").style.color = "var(--present)";
$("sc3").innerHTML = ic("xCircle"); $("sc3").style.background = "var(--absent-soft)"; $("sc3").style.color = "var(--absent)";
$("sc4").innerHTML = ic("clock"); $("sc4").style.background = "var(--late-soft)"; $("sc4").style.color = "var(--late)";
$("sc5").innerHTML = ic("minus"); $("sc5").style.background = "var(--line2)"; $("sc5").style.color = "var(--muted)";
$("dateChip").innerHTML = ic("calendar") + '<span id="chipDate"></span>';

const state = { date:"", students:[] };
const pending = new Map();
let filter = "";

/* ------- الحضور ------- */
async function loadState(){
  const data = await api("/api/state?date=" + encodeURIComponent(state.date));
  state.students = data.students;
  render();
}

function render(){
  const tb = $("tbody");
  tb.innerHTML = "";
  const q = filter.trim().toLowerCase();
  let shown = 0;

  state.students.forEach((s, i) => {
    if(q && !(s.name.toLowerCase().includes(q) || String(s.exam_no).includes(q))) return;
    shown++;
    const tr = document.createElement("tr");
    if(pending.has(s.id)) tr.classList.add("pending");

    const tdN = document.createElement("td");
    tdN.textContent = i + 1;

    const tdName = document.createElement("td");
    tdName.textContent = s.name;
    tdName.style.fontWeight = "600";

    const tdExam = document.createElement("td");
    tdExam.innerHTML = '<span class="code"></span>';
    tdExam.firstChild.textContent = s.exam_no;

    const tdPass = document.createElement("td");
    tdPass.innerHTML = '<span class="code"></span>';
    tdPass.firstChild.textContent = s.password;

    const tdSeg = document.createElement("td");
    const seg = document.createElement("div");
    seg.className = "seg";
    [["present","حاضر"],["absent","غائب"],["late","متأخر"]].forEach(([val, txt]) => {
      const b = document.createElement("button");
      b.textContent = txt;
      b.dataset.s = val;
      if(s.status === val) b.classList.add("on");
      b.addEventListener("click", () => setStatus(s.id, val));
      seg.appendChild(b);
    });
    tdSeg.appendChild(seg);

    const tdAct = document.createElement("td");
    const reset = document.createElement("button");
    reset.className = "btn ghost icon sm";
    reset.innerHTML = ic("refresh");
    reset.title = "توليد رمز جديد";
    reset.style.marginInlineEnd = "6px";
    reset.addEventListener("click", () => resetPassword(s.id, s.name));
    const del = document.createElement("button");
    del.className = "btn danger icon sm";
    del.innerHTML = ic("trash");
    del.title = "حذف الطالب";
    del.addEventListener("click", () => deleteStudent(s.id, s.name));
    tdAct.append(reset, del);

    tr.append(tdN, tdName, tdExam, tdPass, tdSeg, tdAct);
    tb.appendChild(tr);
  });

  $("empty").style.display = state.students.length ? "none" : "block";
  $("hint").textContent = q
    ? `عرض ${shown} من ${state.students.length} طالب`
    : `إجمالي ${state.students.length} طالب`;

  const c = { present:0, absent:0, late:0, none:0 };
  state.students.forEach(s => c[s.status] = (c[s.status]||0) + 1);
  $("cTotal").textContent = state.students.length;
  $("cPresent").textContent = c.present;
  $("cAbsent").textContent = c.absent;
  $("cLate").textContent = c.late;
  $("cNone").textContent = c.none;
}

function setStatus(id, val){
  const s = state.students.find(x => x.id === id);
  if(!s) return;
  s.status = (s.status === val ? "none" : val);
  pending.set(id, s.status);
  render();
  updateSaveBar();
}

function updateSaveBar(){
  const n = pending.size;
  $("savebar").classList.toggle("show", n > 0);
  $("saveInfo").textContent = n
    ? `يوجد ${n} تغيير غير محفوظ`
    : "لا توجد تغييرات";
}

async function saveAttendance(){
  if(!pending.size){ toast("لا توجد تغييرات للحفظ", "ok"); return; }
  const records = {};
  pending.forEach((v, k) => { records[k] = v; });
  await api("/api/attendance", {
    method:"POST",
    headers:{ "Content-Type":"application/json" },
    body: JSON.stringify({ date: state.date, records })
  });
  pending.clear();
  updateSaveBar();
  await loadState();
  toast("تم حفظ الحضور بنجاح", "ok");
}

function markAllPresent(){
  if(!state.students.length){ toast("لا يوجد طلاب", "err"); return; }
  state.students.forEach(s => { s.status = "present"; pending.set(s.id, "present"); });
  render();
  updateSaveBar();
  toast("حُدِّد الجميع حاضر — اضغط حفظ التغييرات");
}

async function addStudent(){
  const name = $("name").value.trim();
  const exam_no = $("exam").value.trim();
  if(!name){ toast("اكتب اسم الطالب", "err"); $("name").focus(); return; }
  if(!exam_no){ toast("اكتب الرقم الامتحاني", "err"); $("exam").focus(); return; }
  try{
    const r = await api("/api/students", {
      method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ name, exam_no })
    });
    $("name").value = ""; $("exam").value = ""; $("name").focus();
    await loadState();
    toast(`تمت إضافة ${name} · رمزه ${r.student.password}`, "ok");
  }catch(e){ toast(e.message, "err"); }
}

async function resetPassword(id, name){
  if(!confirm("توليد رمز جديد للطالب: " + name + " ؟")) return;
  try{
    const r = await api("/api/students/" + id + "/password", { method:"POST" });
    await loadState();
    toast(`رمز ${name} الجديد: ${r.student.password}`, "ok");
  }catch(e){ toast(e.message, "err"); }
}

async function deleteStudent(id, name){
  if(!confirm("هل تريد حذف الطالب: " + name + " ؟")) return;
  try{
    await api("/api/students/" + id, { method:"DELETE" });
    pending.delete(id);
    updateSaveBar();
    await loadState();
    toast("تم حذف " + name, "ok");
  }catch(e){ toast(e.message, "err"); }
}

/* ------- المحاضرات ------- */
async function loadLectures(){
  const data = await api("/api/lectures");
  const tb = $("lecBody");
  tb.innerHTML = "";
  data.lectures.forEach(l => {
    const tr = document.createElement("tr");
    const tdT = document.createElement("td");
    tdT.textContent = l.title;
    tdT.style.fontWeight = "600";
    const tdF = document.createElement("td");
    tdF.textContent = l.filename;
    const tdS = document.createElement("td");
    tdS.textContent = fmtSize(l.size);
    const tdA = document.createElement("td");
    const dl = document.createElement("a");
    dl.className = "btn ghost icon sm";
    dl.href = "/download/" + l.id;
    dl.innerHTML = ic("download");
    dl.title = "تنزيل";
    dl.style.marginInlineEnd = "6px";
    const del = document.createElement("button");
    del.className = "btn danger icon sm";
    del.innerHTML = ic("trash");
    del.title = "حذف";
    del.addEventListener("click", () => deleteLecture(l.id, l.title));
    tdA.append(dl, del);
    tr.append(tdT, tdF, tdS, tdA);
    tb.appendChild(tr);
  });
  $("lecEmpty").style.display = data.lectures.length ? "none" : "block";
}

async function uploadLecture(){
  const fileInput = $("lecFile");
  if(!fileInput.files.length){ toast("اختر ملفاً أولاً", "err"); return; }
  const fd = new FormData();
  fd.append("file", fileInput.files[0]);
  fd.append("title", $("lecTitle").value.trim());
  const btn = $("lecBtn");
  btn.disabled = true;
  try{
    const r = await api("/api/lectures", { method:"POST", body: fd });
    $("lecTitle").value = ""; fileInput.value = "";
    await loadLectures();
    toast("تم رفع: " + r.lecture.title, "ok");
  }catch(e){ toast(e.message, "err"); }
  finally{ btn.disabled = false; }
}

async function deleteLecture(id, title){
  if(!confirm("حذف المحاضرة: " + title + " ؟")) return;
  try{
    await api("/api/lectures/" + id, { method:"DELETE" });
    await loadLectures();
    toast("تم الحذف", "ok");
  }catch(e){ toast(e.message, "err"); }
}

/* ------- الجدول ------- */
async function loadSchedule(){
  const data = await api("/api/schedule");
  const tb = $("sBody");
  tb.innerHTML = "";
  data.schedule.forEach(r => {
    const tr = document.createElement("tr");
    const tdDay = document.createElement("td");
    tdDay.textContent = r.day;
    tdDay.style.fontWeight = "600";
    const tdTime = document.createElement("td");
    tdTime.textContent = r.time || "--";
    const tdSub = document.createElement("td");
    tdSub.textContent = r.subject;
    const tdRoom = document.createElement("td");
    tdRoom.textContent = r.room || "--";
    const tdDel = document.createElement("td");
    const del = document.createElement("button");
    del.className = "btn danger icon sm";
    del.innerHTML = ic("trash");
    del.addEventListener("click", () => deleteSchedule(r.id));
    tdDel.appendChild(del);
    tr.append(tdDay, tdTime, tdSub, tdRoom, tdDel);
    tb.appendChild(tr);
  });
  $("sEmpty").style.display = data.schedule.length ? "none" : "block";
}

async function addSchedule(){
  const day = $("sDay").value;
  const time = $("sTime").value;
  const subject = $("sSubject").value.trim();
  const room = $("sRoom").value.trim();
  if(!subject){ toast("اكتب اسم المادة", "err"); $("sSubject").focus(); return; }
  try{
    await api("/api/schedule", {
      method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ day, time, subject, room })
    });
    $("sTime").value = ""; $("sSubject").value = ""; $("sRoom").value = "";
    await loadSchedule();
    toast("تمت إضافة الحصة", "ok");
  }catch(e){ toast(e.message, "err"); }
}

async function deleteSchedule(id){
  try{
    await api("/api/schedule/" + id, { method:"DELETE" });
    await loadSchedule();
    toast("تم الحذف", "ok");
  }catch(e){ toast(e.message, "err"); }
}

/* ------- التشغيل ------- */
function init(){
  wireTabs();
  $("logout").addEventListener("click", doLogout);

  $("date").value = todayISO();
  state.date = $("date").value;
  $("chipDate").textContent = state.date;

  $("addBtn").addEventListener("click", addStudent);
  $("exam").addEventListener("keydown", e => { if(e.key === "Enter") addStudent(); });
  $("search").addEventListener("input", e => { filter = e.target.value; render(); });
  $("allPresent").addEventListener("click", markAllPresent);
  $("lecBtn").addEventListener("click", uploadLecture);
  $("sBtn").addEventListener("click", addSchedule);
  $("saveBtn").addEventListener("click", () => saveAttendance().catch(e => toast(e.message, "err")));
  $("discardBtn").addEventListener("click", async () => {
    pending.clear(); updateSaveBar();
    try{ await loadState(); toast("تم إلغاء التغييرات", "ok"); }catch(e){ toast(e.message, "err"); }
  });

  $("date").addEventListener("change", async e => {
    const nd = e.target.value;
    if(!nd){ return; }
    if(pending.size){
      const doSave = confirm("لديك تغييرات غير محفوظة.\n«موافق» = حفظها ثم الانتقال، «إلغاء» = تجاهلها والانتقال.");
      if(doSave){
        try{ await saveAttendance(); }
        catch(err){ toast(err.message, "err"); e.target.value = state.date; return; }
      }else{
        pending.clear(); updateSaveBar();
      }
    }
    state.date = nd;
    $("chipDate").textContent = nd;
    try{ await loadState(); }catch(err){ toast(err.message, "err"); }
  });

  window.addEventListener("beforeunload", e => {
    if(pending.size){ e.preventDefault(); e.returnValue = ""; }
  });

  updateSaveBar();
  loadState().catch(e => toast(e.message, "err"));
  loadLectures().catch(e => toast(e.message, "err"));
  loadSchedule().catch(e => toast(e.message, "err"));
}

init();
</script>
</body>
</html>
""".replace("__DAY_OPTIONS__", DAY_OPTIONS)


# ---------------------------------------------------------------------------
# صفحة الطالب
# ---------------------------------------------------------------------------
STUDENT_PAGE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>صفحة الطالب</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%234f46e5'%3E%3Cpath d='M22 10L12 5 2 10l10 5 10-5z'/%3E%3Cpath d='M6 12v5c0 1.5 2.7 3 6 3s6-1.5 6-3v-5' fill='none' stroke='%234f46e5' stroke-width='2'/%3E%3C/svg%3E">
<link rel="stylesheet" href="/static/app.css">
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand">
      <span class="logo" id="brandLogo"></span>
      <div><b>نظام الحضور</b><small>صفحة الطالب</small></div>
    </div>

    <nav class="nav">
      <button class="nav-item on" data-tab="me" data-title="حضوري وغيابي">
        <span id="nav1"></span><span>حضوري وغيابي</span>
      </button>
      <button class="nav-item" data-tab="lectures" data-title="المحاضرات">
        <span id="nav2"></span><span>المحاضرات</span>
      </button>
      <button class="nav-item" data-tab="schedule" data-title="الجدول الدراسي">
        <span id="nav3"></span><span>الجدول الدراسي</span>
      </button>
    </nav>

    <div class="side-foot">
      <div class="side-user">
        <span class="avatar" id="ava"></span>
        <div><b id="sideName">··</b><small id="sideExam">··</small></div>
      </div>
      <button class="btn ghost sm" id="logout"><span id="out1"></span>خروج</button>
    </div>
  </aside>

  <main class="main">
    <header class="topbar">
      <h2 id="pageTitle">حضوري وغيابي</h2>
      <span class="chip" id="meChip"></span>
    </header>

    <div class="content">

      <section class="tabpane on" data-pane="me">
        <div class="stats">
          <div class="stat">
            <span class="statchip" id="sc1"></span>
            <div><b id="cPresent">0</b><span>أيام الحضور</span></div>
          </div>
          <div class="stat">
            <span class="statchip" id="sc2"></span>
            <div><b id="cAbsent">0</b><span>أيام الغياب</span></div>
          </div>
          <div class="stat">
            <span class="statchip" id="sc3"></span>
            <div><b id="cLate">0</b><span>أيام التأخير</span></div>
          </div>
        </div>

        <div class="card table-card">
          <table>
            <thead>
              <tr>
                <th style="width:170px">التاريخ</th>
                <th>الحالة</th>
              </tr>
            </thead>
            <tbody id="attBody"></tbody>
          </table>
          <div class="empty" id="attEmpty" style="display:none"></div>
        </div>
      </section>

      <section class="tabpane" data-pane="lectures">
        <div class="card table-card">
          <table>
            <thead>
              <tr>
                <th>العنوان</th>
                <th>الملف</th>
                <th style="width:120px">الحجم</th>
                <th style="width:120px">تنزيل</th>
              </tr>
            </thead>
            <tbody id="lecBody"></tbody>
          </table>
          <div class="empty" id="lecEmpty" style="display:none"></div>
        </div>
      </section>

      <section class="tabpane" data-pane="schedule">
        <div class="card table-card">
          <table>
            <thead>
              <tr>
                <th style="width:130px">اليوم</th>
                <th style="width:130px">الوقت</th>
                <th>المادة</th>
                <th style="width:190px">القاعة</th>
              </tr>
            </thead>
            <tbody id="sBody"></tbody>
          </table>
          <div class="empty" id="sEmpty" style="display:none"></div>
        </div>
      </section>
    </div>
  </main>
</div>

<div class="toast" id="toast"></div>
<script src="/static/app.js"></script>
<script>
$("brandLogo").innerHTML = ic("cap");
$("nav1").innerHTML = ic("userCheck");
$("nav2").innerHTML = ic("book");
$("nav3").innerHTML = ic("calendar");
$("ava").innerHTML = ic("user");
$("out1").innerHTML = ic("logout");
$("sc1").innerHTML = ic("checkCircle"); $("sc1").style.background = "var(--present-soft)"; $("sc1").style.color = "var(--present)";
$("sc2").innerHTML = ic("xCircle"); $("sc2").style.background = "var(--absent-soft)"; $("sc2").style.color = "var(--absent)";
$("sc3").innerHTML = ic("clock"); $("sc3").style.background = "var(--late-soft)"; $("sc3").style.color = "var(--late)";
$("attEmpty").innerHTML = ic("inbox") + "لم يُسجّل لك حضور بعد.";
$("lecEmpty").innerHTML = ic("inbox") + "لا توجد محاضرات بعد.";
$("sEmpty").innerHTML = ic("inbox") + "لا توجد حصص بعد.";

async function loadMe(){
  const data = await api("/api/me/attendance");
  $("sideName").textContent = data.name;
  $("sideExam").textContent = "الرقم " + data.exam_no;
  $("meChip").innerHTML = ic("user") + '<span>الرقم ' + data.exam_no + '</span>';

  $("cPresent").textContent = data.counts.present || 0;
  $("cAbsent").textContent = data.counts.absent || 0;
  $("cLate").textContent = data.counts.late || 0;

  const tb = $("attBody");
  tb.innerHTML = "";
  data.rows.forEach(r => {
    const tr = document.createElement("tr");
    const tdD = document.createElement("td");
    tdD.textContent = r.date;
    tdD.style.fontWeight = "600";
    const tdS = document.createElement("td");
    const b = document.createElement("span");
    b.className = "badge " + r.status;
    b.textContent = STATUS_LABEL[r.status] || r.status;
    tdS.appendChild(b);
    tr.append(tdD, tdS);
    tb.appendChild(tr);
  });
  $("attEmpty").style.display = data.rows.length ? "none" : "block";
}

async function loadLectures(){
  const data = await api("/api/lectures");
  const tb = $("lecBody");
  tb.innerHTML = "";
  data.lectures.forEach(l => {
    const tr = document.createElement("tr");
    const tdT = document.createElement("td");
    tdT.textContent = l.title;
    tdT.style.fontWeight = "600";
    const tdF = document.createElement("td");
    tdF.textContent = l.filename;
    const tdS = document.createElement("td");
    tdS.textContent = fmtSize(l.size);
    const tdD = document.createElement("td");
    const a = document.createElement("a");
    a.className = "btn ghost sm";
    a.href = "/download/" + l.id;
    a.innerHTML = ic("download") + "تنزيل";
    tdD.appendChild(a);
    tr.append(tdT, tdF, tdS, tdD);
    tb.appendChild(tr);
  });
  $("lecEmpty").style.display = data.lectures.length ? "none" : "block";
}

async function loadSchedule(){
  const data = await api("/api/schedule");
  const tb = $("sBody");
  tb.innerHTML = "";
  data.schedule.forEach(r => {
    const tr = document.createElement("tr");
    const tdDay = document.createElement("td");
    tdDay.textContent = r.day;
    tdDay.style.fontWeight = "600";
    const tdTime = document.createElement("td");
    tdTime.textContent = r.time || "--";
    const tdSub = document.createElement("td");
    tdSub.textContent = r.subject;
    const tdRoom = document.createElement("td");
    tdRoom.textContent = r.room || "--";
    tr.append(tdDay, tdTime, tdSub, tdRoom);
    tb.appendChild(tr);
  });
  $("sEmpty").style.display = data.schedule.length ? "none" : "block";
}

function init(){
  wireTabs();
  $("logout").addEventListener("click", doLogout);
  loadMe().catch(e => toast(e.message, "err"));
  loadLectures().catch(e => toast(e.message, "err"));
  loadSchedule().catch(e => toast(e.message, "err"));
}

init();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# التشغيل
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="نظام الحضور والمحاضرات والجدول")
    p.add_argument("--host", default=DEFAULT_HOST, help="عنوان التشغيل")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="رقم المنفذ")
    p.add_argument("--no-browser", action="store_true", help="لا تفتح المتصفح تلقائياً")
    return p.parse_args()


def main():
    args = parse_args()
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{args.port}/"

    print("=" * 62)
    print(" نظام الحضور والمحاضرات والجدول يعمل الآن")
    print(f" افتح المتصفح على : {url}")
    print(f" دخول الأدمن       : {ADMIN_USER} / {ADMIN_PASS}")
    print(f" ملف البيانات      : {DATA_FILE}")
    print(f" مجلد المحاضرات    : {LECTURES_DIR}")
    print(" لإيقاف البرنامج اضغط Ctrl+C")
    print("=" * 62)

    if not args.no_browser and args.host in ("127.0.0.1", "localhost"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        app.run(host=args.host, port=args.port, debug=False, threaded=True)
    except KeyboardInterrupt:
        print("\nتم إيقاف البرنامج.")


if __name__ == "__main__":
    main()
