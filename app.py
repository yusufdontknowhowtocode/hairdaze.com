# app.py — Multi-site Hair/Nail template (Supabase + real admin + CSRF + emails)

from flask import (
    Flask, render_template, request, jsonify, redirect, url_for, session
)
import os, ssl, smtplib, threading, time, secrets
from email.message import EmailMessage
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Optional
from dotenv import load_dotenv
from werkzeug.security import check_password_hash

# ---------- Boot ----------
load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
NY_TZ = ZoneInfo("America/New_York")
app.config["TEMPLATES_AUTO_RELOAD"] = True

# ---------- Rate limiting (login, optional) ----------
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    limiter = Limiter(get_remote_address, app=app, storage_uri="memory://")
except Exception:
    class _NoLimiter:
        def limit(self, *_a, **_k):
            def _wrap(f): return f
            return _wrap
    limiter = _NoLimiter()

# ---------- Supabase ----------
from supabase import create_client, Client

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_SERVICE_KEY = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()
SUPABASE_TABLE = os.getenv("SUPABASE_TABLE", "appointments")   # e.g. appointments_nails
DEFAULT_SLUG = os.getenv("DEFAULT_SITE_SLUG", "hairdaze")

assert SUPABASE_URL.startswith("https://") and ".supabase.co" in SUPABASE_URL, "Bad SUPABASE_URL"
assert len(SUPABASE_SERVICE_KEY) > 40, "Missing SUPABASE_SERVICE_KEY"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
print(f"[Supabase] connected • table '{SUPABASE_TABLE}'")

def storage_public_url(path: str) -> str:
    # e.g. site-media/<slug>/<filename> (bucket must be public)
    return f"{SUPABASE_URL}/storage/v1/object/public/{path}"

# ---------- Session guards ----------
def requires_login(f):
    from functools import wraps
    @wraps(f)
    def _wrap(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return _wrap

def requires_csrf(f):
    from functools import wraps
    @wraps(f)
    def _wrap(*args, **kwargs):
        if request.headers.get("X-CSRF-Token") != session.get("csrf"):
            return jsonify({"ok": False, "error": "csrf"}), 403
        return f(*args, **kwargs)
    return _wrap

# ---------- Site loader (business + content) ----------
def load_site(slug: str) -> dict:
    # 1) business
    biz_resp = (supabase.table("businesses")
                .select("*").eq("slug", slug).limit(1).execute())
    biz = (biz_resp.data or [None])[0] or {}
    business_id = biz.get("id")

    # 2) services
    svcs = []
    if business_id:
        r = (supabase.table("services")
             .select("name,description,price,sort_order,published")
             .eq("business_id", business_id).eq("published", True)
             .order("sort_order", desc=False).execute())
        svcs = r.data or []

    # 3) reviews
    revs = []
    if business_id:
        r = (supabase.table("reviews")
             .select("text,author,stars,sort_order,published")
             .eq("business_id", business_id).eq("published", True)
             .order("sort_order", desc=False).execute())
        revs = r.data or []

    # 4) hero images (Supabase Storage) with env fallback
    hero_urls = []
    if business_id:
        r = (supabase.table("hero_images")
             .select("filename,sort_order,published")
             .eq("business_id", business_id).eq("published", True)
             .order("sort_order", desc=False).execute())
        rows = r.data or []
        hero_urls = [
            storage_public_url(f"site-media/{slug}/{row['filename']}")
            for row in rows if row.get("filename")
        ]
    if not hero_urls:
        # fallback to comma-separated env (e.g. "nail1.jpg,nail2.jpg")
        hero_urls = [s.strip() for s in (os.getenv("HERO_IMAGES", "")).split(",") if s.strip()]

    site = {
        "slug": slug,
        "brand": {
            "name": biz.get("name") or os.getenv("SALON_NAME", "HairDaze"),
            "tagline": biz.get("tagline") or os.getenv("TAGLINE", "Where Style Meets Simplicity"),
        },
        "theme": {
            "gradient_start": biz.get("gradient_start") or os.getenv("GRADIENT_START", "#ff9966"),
            "gradient_end":   biz.get("gradient_end")   or os.getenv("GRADIENT_END",   "#66cccc"),
        },
        "hero": {
            "cta_text": biz.get("cta_text") or os.getenv("CTA_TEXT", "Book Now"),
            "cta_url":  biz.get("cta_url")  or os.getenv("CTA_URL",  "/book"),
            "subtext":  os.getenv("HERO_SUBTEXT", "Color, cuts, and styling done with care—and on your schedule."),
            "images":   hero_urls,
        },
        "services": svcs,
        "reviews": revs,
        "contact": {
            "address":   biz.get("address")   or os.getenv("SALON_ADDRESS", "414 E Walnut St, North Wales, PA 19454"),
            "phone":     biz.get("phone")     or os.getenv("SALON_PHONE", ""),
            "email":     biz.get("email")     or os.getenv("SALON_EMAIL", os.getenv("FROM_EMAIL") or ""),
            "map_embed": biz.get("map_embed") or os.getenv("MAP_EMBED", ""),
        },
    }
    return site

# ---------- Appointments helpers ----------
def sb_slot_taken(date_str: str, time_str: str) -> bool:
    r = (supabase.table(SUPABASE_TABLE)
         .select("id").eq("date", date_str).eq("time", time_str)
         .eq("status", "Scheduled").limit(1).execute())
    return bool(r.data)

def sb_insert_booking(date_str: str, time_str: str, name: str, service: str, email: Optional[str]):
    payload = {
        "date": date_str, "time": time_str, "name": name,
        "service": service, "email": (email or None), "status": "Scheduled"
    }
    supabase.table(SUPABASE_TABLE).insert(payload).execute()
    print("[Supabase] booking inserted")

def _fetch_booking(booking_id: int) -> Optional[dict]:
    r = (supabase.table(SUPABASE_TABLE)
         .select("id, date, time, name, service, email, status")
         .eq("id", booking_id).limit(1).execute())
    rows = r.data or []
    return rows[0] if rows else None

def sb_cancel_by_details(date_str: str, time_str: str, name: str, service: str) -> int:
    res = (supabase.table(SUPABASE_TABLE)
           .update({"status": "Cancelled"})
           .eq("date", date_str).eq("time", time_str)
           .eq("name", name).eq("service", service)
           .eq("status", "Scheduled").execute())
    return len(res.data or [])

def sb_cancel_by_id(booking_id: int):
    (supabase.table(SUPABASE_TABLE)
     .update({"status": "Cancelled"})
     .eq("id", booking_id).eq("status", "Scheduled").execute())
    row = _fetch_booking(booking_id)
    changed = bool(row and row.get("status") == "Cancelled")
    return changed, row

def sb_complete_by_id(booking_id: int):
    (supabase.table(SUPABASE_TABLE)
     .update({"status": "Completed"})
     .eq("id", booking_id).eq("status", "Scheduled").execute())
    row = _fetch_booking(booking_id)
    changed = bool(row and row.get("status") == "Completed")
    return changed, row

def sb_update_booking(booking_id: int, name: str, service: str, date_str: str, time_str: str):
    clash = (supabase.table(SUPABASE_TABLE)
             .select("id").eq("date", date_str).eq("time", time_str)
             .eq("status", "Scheduled").neq("id", booking_id).limit(1).execute())
    if clash.data:
        return None, "That time is already booked"
    (supabase.table(SUPABASE_TABLE)
     .update({"name": name, "service": service, "date": date_str, "time": time_str})
     .eq("id", booking_id).execute())
    return _fetch_booking(booking_id), None

def sb_load_appointments(start: Optional[str] = None, end: Optional[str] = None, status: str = "scheduled"):
    q = supabase.table(SUPABASE_TABLE).select("id, date, time, name, email, service, status")
    if start: q = q.gte("date", start)
    if end:   q = q.lte("date", end)
    if status != "all": q = q.eq("status", "Scheduled")
    q = q.order("date", desc=False).order("time", desc=False)
    return q.execute().data or []

# ---------- Hours & slots ----------
HOURS_BY_WEEKDAY = {
    1: ("10:00 AM", "7:00 PM"),  # Tue
    2: ("2:00 PM",  "7:00 PM"),  # Wed
    3: ("10:00 AM", "7:00 PM"),  # Thu
    4: ("9:00 AM",  "2:00 PM"),  # Fri
    5: ("8:00 AM",  "2:00 PM"),  # Sat
}
def generate_time_slots(start: str, end: str, interval_minutes: int):
    fmt = "%I:%M %p"
    from datetime import datetime as dt
    slots, start_dt, end_dt = [], dt.strptime(start, fmt), dt.strptime(end, fmt)
    while start_dt < end_dt:
        slots.append(start_dt.strftime(fmt).lstrip("0"))
        start_dt += timedelta(minutes=interval_minutes)
    return slots

# ---------- Public pages ----------
@app.get("/", endpoint="index")
def index():
    slug = request.args.get("site") or DEFAULT_SLUG
    site = load_site(slug)
    return render_template("index.html", site=site)

@app.get("/home")
def home():
    return redirect(url_for("index"))

@app.route("/book", methods=["GET", "POST"])
def book():
    if request.method == "POST":
        date_str = (request.form.get("date") or request.form.get("appointment_date") or "").strip()
        time_str = (request.form.get("time") or request.form.get("appointment_time") or "").strip()
        name     = (request.form.get("name") or request.form.get("client_name") or "").strip()
        service  = (request.form.get("service") or "").strip()
        email    = (request.form.get("email") or request.form.get("client_email") or "").strip()

        if not all([date_str, time_str, name, service]):
            return "Missing fields", 400
        if sb_slot_taken(date_str, time_str):
            return "Time already booked", 400

        try:
            sb_insert_booking(date_str, time_str, name, service, email)
            threading.Thread(
                target=lambda: send_booking_confirmation({
                    "name": name, "email": email, "service": service,
                    "date": date_str, "time": time_str
                }),
                daemon=True
            ).start()
        except Exception as e:
            print("[Supabase] insert failed:", e)
            return "Server error — please try again.", 500

        return render_template("confirmation.html", date=date_str, time=time_str, name=name, service=service)

    return render_template("book.html", min_date=date.today().isoformat())

@app.get("/confirmation")
def confirmation():
    return render_template("confirmation.html",
        date=request.args.get("date"),
        time=request.args.get("time"),
        name=request.args.get("name"),
        service=request.args.get("service"),
    )

# ---------- Availability APIs ----------
@app.get("/available_times")
def available_times():
    date_str = request.args.get("date")
    if not date_str:
        return jsonify({"times": []})
    try:
        weekday = datetime.strptime(date_str, "%Y-%m-%d").weekday()
    except ValueError:
        return jsonify({"times": []})
    if weekday not in HOURS_BY_WEEKDAY:
        return jsonify({"times": []})

    start_time, end_time = HOURS_BY_WEEKDAY[weekday]
    all_slots = generate_time_slots(start_time, end_time, 30)

    rows = (supabase.table(SUPABASE_TABLE)
            .select("time").eq("date", date_str).eq("status", "Scheduled")
            .execute().data or [])
    booked = {r["time"] for r in rows}
    open_slots = [s for s in all_slots if s not in booked]
    return jsonify({"times": open_slots})

@app.get("/available_days")
def available_days():
    today_dt = datetime.now(NY_TZ).date()
    start = today_dt.isoformat()
    end = (today_dt + timedelta(days=59)).isoformat()

    rows = (supabase.table(SUPABASE_TABLE)
            .select("date, time").gte("date", start).lte("date", end)
            .eq("status", "Scheduled").execute().data or [])
    booked_by_date = {}
    for r in rows:
        booked_by_date.setdefault(r["date"], set()).add(r["time"])

    result = []
    for i in range(60):
        d = today_dt + timedelta(days=i)
        date_str = d.strftime("%Y-%m-%d")
        wd = d.weekday()
        if wd not in HOURS_BY_WEEKDAY:
            continue
        start_time, end_time = HOURS_BY_WEEKDAY[wd]
        all_slots = generate_time_slots(start_time, end_time, 30)
        if any(s not in booked_by_date.get(date_str, set()) for s in all_slots):
            result.append(date_str)
    return jsonify({"dates": result})

# ---------- Public cancel ----------
@app.post("/cancel")
def cancel_public():
    changed = sb_cancel_by_details(
        request.form["date"], request.form["time"],
        request.form["name"], request.form["service"]
    )
    msg = "✅ Booking cancelled." if changed else "⚠️ Booking not found."
    return render_template("cancelled.html", message=msg)

# ---------- Login / Logout ----------
@app.get("/login")
def login():
    return render_template("login.html")

@limiter.limit("5/minute;50/hour")
@app.post("/login")
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""

    # Option 1: env-based admin (quick)
    env_email = (os.getenv("ADMIN_EMAIL") or "").strip().lower()
    env_pw    = os.getenv("ADMIN_PASSWORD") or ""
    if env_email and env_pw and email == env_email:
        if password == env_pw:
            session.clear()
            session["user_id"] = 1
            session["email"] = env_email
            session["csrf"] = secrets.token_urlsafe(32)
            return redirect(url_for("admin"))
        return render_template("login.html", error="Invalid credentials"), 401

    # Option 2: Supabase-backed admin
    row_res = (supabase.table("admins")
               .select("id, email, password_hash")
               .eq("email", email).limit(1).execute())
    row = (row_res.data or [None])[0]
    if not row or not check_password_hash(row["password_hash"], password):
        return render_template("login.html", error="Invalid email or password"), 401

    session.clear()
    session["user_id"] = row["id"]
    session["email"] = row["email"]
    session["csrf"] = secrets.token_urlsafe(32)
    return redirect(url_for("admin"))

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------- Admin view ----------
@app.get("/admin")
@requires_login
def admin():
    view = request.args.get("view", "today")  # today | all
    today = date.today().isoformat()
    start = today if view == "today" else None
    rows = sb_load_appointments(start=start, status=request.args.get("status","scheduled"))
    return render_template("admin_cloud.html", rows=rows, today=today, view=view, csrf=session.get("csrf"))

# ---------- Admin APIs (session + CSRF) ----------
@app.post("/admin/api/cancel/<int:booking_id>")
@requires_login
@requires_csrf
def admin_api_cancel(booking_id):
    changed, row = sb_cancel_by_id(booking_id)
    if changed and row:
        threading.Thread(target=send_cancellation_email, args=(row,), daemon=True).start()
    return jsonify({"ok": True, "changed": changed})

@app.post("/admin/api/complete/<int:booking_id>")
@requires_login
@requires_csrf
def admin_api_complete(booking_id):
    changed, row = sb_complete_by_id(booking_id)
    if changed and row:
        threading.Thread(target=send_thanks_email, args=(row,), daemon=True).start()
    return jsonify({"ok": True, "changed": changed})

@app.post("/admin/api/update/<int:booking_id>")
@requires_login
@requires_csrf
def admin_api_update(booking_id):
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    service = (data.get("service") or "").strip()
    new_date = (data.get("date") or "").strip()
    new_time = (data.get("time") or "").strip()
    if not all([name, service, new_date, new_time]):
        return jsonify({"ok": False, "message": "Missing fields"}), 400

    row, err = sb_update_booking(booking_id, name, service, new_date, new_time)
    if err:
        return jsonify({"ok": False, "message": err}), 409
    return jsonify({"ok": True, "booking": row})

# ---------- Email (reminders + on cancel/complete/booking) ----------
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
FROM_EMAIL = os.getenv("FROM_EMAIL") or SMTP_USER
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "1") == "1"

SEND_CUSTOMER_NOTIFICATIONS = os.getenv("SEND_CUSTOMER_NOTIFICATIONS", "1") == "1"
SALON_NAME = os.getenv("SALON_NAME", "HairDaze")
SALON_ADDRESS = os.getenv("SALON_ADDRESS", "414 E Walnut St, North Wales, PA 19454")

def send_email(to_email: Optional[str], subject: str, body: str) -> bool:
    if not (EMAIL_ENABLED and SMTP_HOST and SMTP_USER and SMTP_PASS and FROM_EMAIL) or not to_email:
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(body)
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=ctx)
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    return True

def _fmt_appt_line(appt: dict) -> str:
    return f"{appt.get('date')} at {appt.get('time')} — {appt.get('service')}"

def send_booking_confirmation(appt: dict) -> bool:
    if not SEND_CUSTOMER_NOTIFICATIONS: return False
    email = (appt.get("email") or "").strip()
    if not email: return False
    name = appt.get("name") or "there"
    subject = f"Your {SALON_NAME} appointment is booked!"
    body = "\n".join([
        f"Hi {name},", "", "Thanks for booking with us. Here are your details:",
        f"• {_fmt_appt_line(appt)}", "",
        "If you need to make changes, just reply to this email.", "",
        f"See you soon,", SALON_NAME, SALON_ADDRESS,
    ])
    return send_email(email, subject, body)

def send_cancellation_email(appt: dict) -> bool:
    if not SEND_CUSTOMER_NOTIFICATIONS: return False
    email = (appt.get("email") or "").strip()
    if not email: return False
    name = appt.get("name") or "there"
    subject = f"{SALON_NAME}: Your appointment was cancelled"
    body = "\n".join([
        f"Hi {name},", "", "Your appointment has been cancelled:",
        f"• {_fmt_appt_line(appt)}", "",
        "If this was unexpected or you’d like to rebook, just reply to this email.", "",
        f"— {SALON_NAME}", SALON_ADDRESS,
    ])
    return send_email(email, subject, body)

def send_thanks_email(appt: dict) -> bool:
    if not SEND_CUSTOMER_NOTIFICATIONS: return False
    email = (appt.get("email") or "").strip()
    if not email: return False
    name = appt.get("name") or "there"
    subject = f"Thanks for visiting {SALON_NAME}!"
    body = "\n".join([
        f"Hi {name},",
        "",
        "Thanks for coming in today — we hope you loved your service!",
        f"• {_fmt_appt_line(appt)}",
        "",
        "If there’s anything we can do better, just reply to this email.",
        "",
        f"Can’t wait to see you again,",
        SALON_NAME,
        SALON_ADDRESS,
    ])
    return send_email(email, subject, body)

def send_tomorrow_reminders():
    """Daily reminder emails using current SALON_NAME + SALON_ADDRESS."""
    today_local = datetime.now(NY_TZ).date()
    target = (today_local + timedelta(days=1)).strftime("%Y-%m-%d")
    rows = (supabase.table(SUPABASE_TABLE)
            .select("name, email, service, time")
            .eq("date", target).eq("status", "Scheduled")
            .order("time", desc=False).execute().data or [])

    grouped = {}
    for r in rows:
        em = (r.get("email") or "").strip()
        if not em:
            continue
        grouped.setdefault(em, {"name": r["name"], "items": []})
        grouped[em]["items"].append((r["time"], r["service"]))

    for em, data in grouped.items():
        lines = [f"Hi {data['name']},", "", f"Reminder: your {SALON_NAME} appointment(s) tomorrow:"]
        for t, svc in data["items"]:
            lines.append(f"• {t} — {svc}")
        lines += [
            "",
            f"Date: {target}",
            "",
            SALON_ADDRESS,
            "If you need to cancel, just reply to this email.",
            "",
            "See you soon!",
            f"- {SALON_NAME}",
        ]
        send_email(em, f"Reminder: Your {SALON_NAME} appointment(s) tomorrow", "\n".join(lines))

def schedule_daily_reminders(hour=18, minute=0):
    def runner():
        while True:
            now = datetime.now(NY_TZ)
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            time.sleep(max(1, (target - now).total_seconds()))
            try:
                send_tomorrow_reminders()
            except Exception as e:
                print("[Reminders] Error:", e)
    threading.Thread(target=runner, daemon=True).start()

if os.getenv("ENABLE_REMINDERS", "1") == "1":
    schedule_daily_reminders(
        hour=int(os.getenv("REMINDER_HOUR", "18")),
        minute=int(os.getenv("REMINDER_MINUTE", "0"))
    )

@app.get("/healthz")
def healthz():
    return "ok", 200

# ---------- Global template fallback (when DB has no rows) ----------
def _env_fallback_site():
    hero_images = [s.strip() for s in os.getenv("HERO_IMAGES", "").split(",") if s.strip()]
    return {
        "slug": DEFAULT_SLUG,
        "brand": {
            "name": os.getenv("SALON_NAME", "HairDaze"),
            "tagline": os.getenv("TAGLINE", "Where Style Meets Simplicity"),
        },
        "theme": {
            "gradient_start": os.getenv("GRADIENT_START", "#ff9966"),
            "gradient_end":   os.getenv("GRADIENT_END",   "#66cccc"),
        },
        "hero": {
            "cta_text": os.getenv("CTA_TEXT", "Book Now"),
            "cta_url":  os.getenv("CTA_URL",  "/book"),
            "subtext":  os.getenv("HERO_SUBTEXT", "Color, cuts, and styling done with care—and on your schedule."),
            "images":   hero_images,
        },
        "services": [],
        "reviews": [],
        "contact": {
            "address":   os.getenv("SALON_ADDRESS", "414 E Walnut St, North Wales, PA 19454"),
            "phone":     os.getenv("SALON_PHONE", ""),
            "email":     os.getenv("SALON_EMAIL", os.getenv("FROM_EMAIL") or ""),
            "map_embed": os.getenv("MAP_EMBED", ""),
        },
    }

@app.context_processor
def inject_site():
    # Used by pages like /book which don't call load_site() explicitly.
    return {"site": _env_fallback_site()}

# ---------- Run (dev only; Render uses gunicorn) ----------
if __name__ == "__main__":
    # In production: gunicorn app:app -b 0.0.0.0:$PORT --workers 2 --threads 4
    app.run(host="0.0.0.0", port=5002, debug=True)
