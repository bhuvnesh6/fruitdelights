from flask import Flask, render_template, request, redirect, session, url_for, flash, jsonify, send_from_directory
from pymongo import MongoClient
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import os, re
from bson.objectid import ObjectId
import secrets
from datetime import datetime, timedelta, date
import calendar
from functools import wraps
from wp import send_whatsapp
from outwpmsg import send_whatsapp_msg
import cloudinary
import cloudinary.uploader
import threading, time, random


load_dotenv()

cloudinary.config(
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key    = os.getenv("CLOUDINARY_API_KEY"),
    api_secret = os.getenv("CLOUDINARY_API_SECRET"),
    secure     = True,
)

app = Flask(__name__)
app.permanent_session_lifetime = timedelta(days=60) 
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key-change-in-production")

app.config["CLOUDINARY_CLOUD_NAME"]    = os.getenv("CLOUDINARY_CLOUD_NAME", "")
app.config["CLOUDINARY_UPLOAD_PRESET"] = os.getenv("CLOUDINARY_UPLOAD_PRESET", "")

# Mongo Config
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME   = os.getenv("DB_NAME")
client    = MongoClient(MONGO_URI)
db        = client[DB_NAME]

# Collections
users_col        = db.users
teams_col        = db.teams
plans_col        = db.plans
deliveries_col    = db.deliveries
invoices_col      = db.invoices
enquiries_col     = db.enquiries
wa_inbox_col      = db.wa_inbox
manual_bills_col  = db.manual_bills   # NEW: manual billing records
franchises_col = db.franchises
try:
    franchises_col.create_index([("subadmin_id", 1)], unique=True, sparse=True)
except Exception as e:
    print(f"[Franchise] Could not ensure index on franchises: {e}")

# NEW: distributed scheduler lock collection. This is what makes the
# background scheduler safe to run from MULTIPLE worker processes
# (Gunicorn/uWSGI workers, PM2 clusters, etc.) and safe across restarts.
# Only ONE process can ever "win" the lock for a given job+day.
scheduler_locks_col = db.scheduler_locks
try:
    scheduler_locks_col.create_index([("job", 1), ("date", 1)], unique=True)
except Exception as e:
    print(f"[Scheduler] Could not ensure unique index on scheduler_locks: {e}")


# ---------------------------------------------------------------------------
# PLAN SCHEDULE HELPERS
# ---------------------------------------------------------------------------

# Plans with fixed working-day schedules keyed by duration.
# 22-day plan = Mon-Fri only  → exclude Sat + Sun
# 26-day plan = Mon-Sat only  → exclude Sun only
# Any other duration          → no structural exclusion (use customer off_days/holidays only)
PLAN_SCHEDULE_EXCLUSIONS = {
    22: ["Saturday", "Sunday"],
    26: ["Sunday"],
}


def _plan_excluded_days(plan):
    """
    Returns weekday names that are structurally excluded for this plan based on its duration.
    e.g. for a 22-day plan: ['Saturday', 'Sunday']
    These are built into the plan itself and are NOT the same as customer off_days.
    """
    duration = int(plan.get("duration_days", 0))
    return PLAN_SCHEDULE_EXCLUSIONS.get(duration, [])


def _day_schedulable_for_plan(plan, target_date, customer_off_days, customer_holidays):
    """
    Returns True if a delivery should happen on target_date for this plan + customer combo.
    Order of checks:
      1. Plan structural exclusions (22-day → no Sat/Sun, 26-day → no Sun)
      2. Customer off_days
      3. Customer holidays

    NOTE: This is date-driven, not calendar-month-driven. A 22-day plan
    starting on the 3rd of a month simply counts 22 valid (non Sat/Sun) days
    forward from the start date — if that spills into the next calendar
    month, it spills naturally. No special-casing needed here; this is
    already handled correctly by _calculate_invoice_period() which just adds
    (duration_days - 1) days to the start date regardless of month boundary.
    """
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)

    day_name = target_date.strftime("%A")
    date_str = target_date.strftime("%Y-%m-%d")

    if day_name in _plan_excluded_days(plan):
        return False
    if day_name in (customer_off_days or []):
        return False
    if date_str in (customer_holidays or set()):
        return False

    return True


# ---------------------------------------------------------------------------
# DECORATORS
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


# In app.py — replace role_required decorator
def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user_role = session.get("role")
            # Managers AND Subadmins behave as full admins for their own scope
            if user_role in ("manager", "subadmin") and "admin" in roles:
                return f(*args, **kwargs)
            if user_role not in roles:
                flash("Unauthorized access.", "danger")
                return redirect(url_for("home"))
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def super_admin_required(f):
    """Strictly the top-level admin only — used for Franchise management,
    which must stay invisible to subadmins and managers."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if session.get("role") != "admin":
            flash("Unauthorized — restricted to the main admin.", "danger")
            return redirect(url_for("home"))
        return f(*args, **kwargs)
    return decorated_function

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _scoped_team_ids():
    """
    Returns the list of Team ObjectIds visible to the current session.
    Deliveries are stored with a team_id (not franchise_id), so anything
    scoping deliveries_col must go through team_id, not franchise_id.
      - admin    -> all teams with franchise_id None (HQ only)
      - subadmin -> all teams with franchise_id == their own franchise
      - manager  -> only their own assigned team(s)
    """
    role = session.get("role")
    if role == "manager":
        user = users_col.find_one({"_id": ObjectId(session["user_id"])})
        tids = (user.get("team_ids") or ([user["team_id"]] if user and user.get("team_id") else [])) if user else []
        return tids
    ff = _franchise_filter()
    teams = list(teams_col.find(ff, {"_id": 1}))
    return [t["_id"] for t in teams]


def _safe_send_whatsapp(phone, message, context=""):
    """
    Wrapper around wp.send_whatsapp with actual error visibility.
    Every system WhatsApp send (credentials, invoices, manual bills,
    payment reminders, admin summary) goes through this instead of calling
    send_whatsapp directly — a bare `False` return with zero explanation is
    exactly why manual bills / invoices silently "don't reach WhatsApp".

    NOTE: this is a DIFFERENT WhatsApp session/number than
    outwpmsg.send_whatsapp_msg (used only for the enquiry inbox chat).
    The inbox chat working does NOT mean this session is working — if
    system messages stop arriving, check the session behind wp.py first.
    """
    try:
        ok = send_whatsapp(phone, message)
        if not ok:
            print(f"[WA-FAIL] {context or 'send'} to {phone} — wp.send_whatsapp "
                  f"returned falsy. Check that Number A's WhatsApp session "
                  f"(the one wp.py talks to) is still connected/logged in.")
        return ok
    except Exception as e:
        print(f"[WA-ERROR] {context or 'send'} to {phone} raised: {e}")
        return False


# ---------------------------------------------------------------------------
# FRANCHISE / MULTI-TENANT SCOPING HELPERS
# ---------------------------------------------------------------------------
def _current_user_doc():
    if "user_id" not in session:
        return None
    return users_col.find_one({"_id": ObjectId(session["user_id"])})


def _current_franchise_id():
    """None => top-level admin's own HQ business. Otherwise the ObjectId of
    the franchise the logged-in subadmin/manager/employee/customer belongs to."""
    role = session.get("role")
    if role == "admin":
        return None
    user = _current_user_doc()
    return user.get("franchise_id") if user else None


def _franchise_filter():
    """Scopes collections keyed only by franchise_id: plans, teams, the
    manager list. Top admin -> HQ only. Subadmin/manager -> their franchise."""
    role = session.get("role")
    if role == "admin":
        return {"franchise_id": None}
    if role in ("subadmin", "manager"):
        return {"franchise_id": _current_franchise_id()}
    return {}


def _norm_city_key(city):
    return (city or "").strip().lower()

def _get_admin_whatsapp_numbers():
    """
    Supports ADMIN_WHATSAPP_NUMBERS="9876543210,9123456780" (comma-separated)
    so admin broadcasts (boxes-tomorrow summary, etc.) can go to BOTH admin
    phones. Falls back to the old single ADMIN_WHATSAPP_NUMBER if the plural
    var isn't set.
    """
    raw = os.getenv("ADMIN_WHATSAPP_NUMBERS") or os.getenv("ADMIN_WHATSAPP_NUMBER", "")
    numbers = []
    for part in raw.split(","):
        digits = "".join(filter(str.isdigit, part.strip()))
        if not digits:
            continue
        if len(digits) == 10:
            digits = "91" + digits
        numbers.append(digits)
    return numbers


def send_credentials_whatsapp(email, password, name, phone):
    phone = "".join(filter(str.isdigit, str(phone)))
    if len(phone) == 10:
        phone = "91" + phone
    msg = (
        f"🍓 Fruit Delights\n\n"
        f"Hello {name},\n\n"
        f"Your account has been created.\n\n"
        f"Login ID: {email}\n"
        f"Password: {password}\n\n"
        f"Login:\nhttps://fruitedelights.com/login\n\n"
        f"Thank you."
    )
    _safe_send_whatsapp(phone, msg, context="credentials")

def send_franchise_credentials_whatsapp(email, password, name, phone, business_name, city, address):
    phone = "".join(filter(str.isdigit, str(phone)))
    if len(phone) == 10:
        phone = "91" + phone
    msg = (
        f"🍓 Fruit Delights – Franchise Onboarding\n\n"
        f"Hello {name},\n\n"
        f"Your Franchise '{business_name}' has been created! 🎉\n"
        f"📍 City: {city}\n"
        f"🏠 Address: {address or '—'}\n\n"
        f"Login ID: {email}\n"
        f"Password: {password}\n\n"
        f"Login:\nhttps://fruitedelights.com/login\n\n"
        f"You have full admin access to manage plans, teams, employees, "
        f"managers and customers for your own franchise business.\n\n"
        f"Thank you."
    )
    _safe_send_whatsapp(phone, msg, context="franchise_credentials")

def _generate_customer_login_id(name: str) -> str:
    """
    Generate a short login ID: firstname (max 8 chars) + 4-char hex @ fd
    e.g. rahul4a2c@fd   (short, clean, unique)
    """
    parts = re.sub(r'[^a-zA-Z0-9 ]', '', name).strip().split()
    base  = parts[0][:8].lower() if parts else "user"
    suffix    = secrets.token_hex(2)
    candidate = f"{base}{suffix}@fd"
    while users_col.find_one({"email": candidate}):
        candidate = f"{base}{secrets.token_hex(2)}@fd"
    return candidate


def _resolve_customer_email(phone, existing_email=""):
    email = (existing_email or "").strip()
    if email:
        return email
    safe_phone = "".join(filter(str.isdigit, phone or "unknown"))
    generated  = f"{safe_phone}@fd"
    if users_col.find_one({"email": generated}):
        generated = f"{safe_phone}_{secrets.token_hex(2)}@fd"
    return generated


def _manager_team_filter():
    role = session.get("role")
    if role == "manager":
        user = users_col.find_one({"_id": ObjectId(session["user_id"])})
        if not user:
            return {"team_id": None}
        tids = user.get("team_ids") or ([user["team_id"]] if user.get("team_id") else [])
        conditions = [{"assigned_manager_id": user["_id"]}]
        if tids:
            conditions.append({"team_id": {"$in": tids}})
        f = {"$or": conditions}
        f["franchise_id"] = user.get("franchise_id")
        return f
    # admin / subadmin fall back to plain franchise scoping (HQ vs own franchise)
    return _franchise_filter()

def _enrich_delivery_plan(delivery_dict: dict, cust: dict, today_date) -> dict:
    """Adds plan_name, is_alternate_plan, today_item to a delivery dict.
    Per-customer alt_options takes priority over plan-level alternate_items."""
    plan = None
    if cust.get("plan_id"):
        plan = plans_col.find_one({"_id": ObjectId(cust["plan_id"])})

    if isinstance(today_date, str):
        today_date = date.fromisoformat(today_date)

    # Per-customer sequence overrides plan sequence
    cust_alt = cust.get("alt_options", [])
    if cust_alt:
        today_item = _get_customer_item_for_date(cust, today_date)
        is_alt     = True
    elif plan:
        alt_items  = plan.get("alternate_items", [])
        is_alt     = bool(alt_items and len(alt_items) >= 2)
        today_item = _get_alternate_item_for_date(plan, today_date) if is_alt else None
    else:
        is_alt     = False
        today_item = None

    has_personal_alt = bool(cust.get("alt_options", []))

    if plan:
        delivery_dict["plan_name"]          = plan.get("name", "")
        delivery_dict["is_alternate_plan"]  = is_alt
        delivery_dict["has_personal_alt"]   = has_personal_alt
        delivery_dict["today_item"]         = today_item
        delivery_dict["plan_id_str"]        = str(plan["_id"])
    else:
        delivery_dict["plan_name"]          = cust.get("sample", "")
        delivery_dict["is_alternate_plan"]  = is_alt
        delivery_dict["has_personal_alt"]   = has_personal_alt
        delivery_dict["today_item"]         = today_item

    return delivery_dict


# ---------------------------------------------------------------------------
# INVOICE HELPERS
# ---------------------------------------------------------------------------
def _parse_start_date(customer):
    sd = customer.get("start_date")
    if isinstance(sd, datetime):
        return sd.date()
    if isinstance(sd, date):
        return sd
    if isinstance(sd, str):
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                return datetime.strptime(sd[:len(fmt)], fmt).date()
            except Exception:
                continue
    return None


def _calculate_invoice_period(customer, plan):
    duration_days = int(plan.get("duration_days", 0))
    start         = _parse_start_date(customer)
    if not start or duration_days <= 0:
        today        = date.today()
        period_start = date(today.year, today.month, 1)
        _, last_day  = calendar.monthrange(today.year, today.month)
        period_end   = date(today.year, today.month, last_day)
        return period_start, period_end
    period_start = start
    period_end = start + timedelta(days=duration_days - 1)
    return period_start, period_end


def _billable_days_in_period(customer, period_start, period_end):
    """
    Count schedulable days (respecting plan structure + customer off_days/holidays)
    and actually delivered days within the period.
    """
    plan     = plans_col.find_one({"_id": ObjectId(customer["plan_id"])}) if customer.get("plan_id") else None
    off_days = customer.get("off_days", [])
    holidays = set(customer.get("holidays", []))
    schedulable = 0
    delivered   = 0
    current     = period_start
    while current <= period_end:
        if plan:
            if not _day_schedulable_for_plan(plan, current, off_days, holidays):
                current += timedelta(days=1)
                continue
        else:
            day_name = current.strftime("%A")
            date_str = current.strftime("%Y-%m-%d")
            if day_name in off_days or date_str in holidays:
                current += timedelta(days=1)
                continue
        schedulable += 1
        delivery = deliveries_col.find_one({
            "customer_id": ObjectId(customer["_id"]),
            "date":        current.strftime("%Y-%m-%d"),
            "status":      "delivered"
        })
        if delivery:
            delivered += 1
        current += timedelta(days=1)
    return schedulable, delivered


def _apply_discount(gross, disc_pct, disc_amt):
    discount = 0.0
    if disc_pct and float(disc_pct) > 0:
        discount += gross * (float(disc_pct) / 100)
    if disc_amt and float(disc_amt) > 0:
        discount += float(disc_amt)
    return round(discount, 2), round(max(gross - discount, 0), 2)


def generate_monthly_bill(customer, month=None, year=None):
    plan = plans_col.find_one({"_id": ObjectId(customer["plan_id"])}) if customer.get("plan_id") else None
    if not plan:
        return 0.0
    price_total              = float(plan.get("price_per_month", plan.get("price_per_day", 0)))
    tax_pct                  = float(plan.get("tax_percent", 0))
    period_start, period_end = _calculate_invoice_period(customer, plan)
    schedulable, delivered   = _billable_days_in_period(customer, period_start, period_end)
    if schedulable == 0:
        return 0.0
    subtotal = (delivered / schedulable) * price_total
    tax      = subtotal * (tax_pct / 100)
    gross    = subtotal + tax
    disc_pct = float(customer.get("discount_percent", 0) or 0)
    disc_amt = float(customer.get("discount_amount",  0) or 0)
    _, total = _apply_discount(gross, disc_pct, disc_amt)
    return round(total, 2)


def estimate_upcoming_bill(customer, month=None, year=None):
    plan = plans_col.find_one({"_id": ObjectId(customer["plan_id"])}) if customer.get("plan_id") else None
    if not plan:
        return {"days": 0, "subtotal": 0, "tax": 0, "discount": 0, "total": 0}
    price_total              = float(plan.get("price_per_month", plan.get("price_per_day", 0)))
    tax_pct                  = float(plan.get("tax_percent", 0))
    period_start, period_end = _calculate_invoice_period(customer, plan)
    off_days  = customer.get("off_days", [])
    holidays  = set(customer.get("holidays", []))
    billable_days = 0
    current = period_start
    while current <= period_end:
        if _day_schedulable_for_plan(plan, current, off_days, holidays):
            billable_days += 1
        current += timedelta(days=1)
    subtotal = round(price_total, 2)
    tax      = round(subtotal * tax_pct / 100, 2)
    gross    = subtotal + tax
    disc_pct = float(customer.get("discount_percent", 0) or 0)
    disc_amt = float(customer.get("discount_amount",  0) or 0)
    discount, total = _apply_discount(gross, disc_pct, disc_amt)
    return {
        "days":         billable_days,
        "subtotal":     subtotal,
        "tax":          tax,
        "discount":     discount,
        "total":        total,
        "plan":         plan,
        "period_start": period_start.strftime("%Y-%m-%d"),
        "period_end":   period_end.strftime("%Y-%m-%d"),
    }


def _invoice_due_today(customer):
    plan = plans_col.find_one({"_id": ObjectId(customer["plan_id"])}) if customer.get("plan_id") else None
    if not plan or int(plan.get("duration_days", 0)) <= 0:
        return False
    _, period_end = _calculate_invoice_period(customer, plan)
    return period_end == date.today()


def _send_invoice_whatsapp(customer, invoice):
    phone = "".join(filter(str.isdigit, str(customer.get("phone", ""))))
    if len(phone) == 10:
        phone = "91" + phone
    if not phone:
        return
    period_start = invoice.get("period_start", "")
    period_end   = invoice.get("period_end", "")
    disc_line    = ""
    if invoice.get("discount", 0) > 0:
        disc_line = f"Discount:     -₹{invoice['discount']:.2f}\n"
    msg = (
        f"🍓 Fruit Delights – Invoice\n\n"
        f"Hello {customer.get('name', '')},\n\n"
        f"Your invoice for the period\n"
        f"{period_start} to {period_end}\n\n"
        f"Plan:         {invoice.get('plan_name', '')}\n"
        f"Billable Days:{invoice.get('billable_days', 0)}\n"
        f"Subtotal:     ₹{invoice.get('subtotal', 0):.2f}\n"
        f"Tax:          ₹{invoice.get('tax', 0):.2f}\n"
        f"{disc_line}"
        f"─────────────────\n"
        f"Total Due:    ₹{invoice.get('total', 0):.2f}\n\n"
        f"Thank you for being a valued customer! 🍉"
    )
    _safe_send_whatsapp(phone, msg, context="invoice")


# ---------------------------------------------------------------------------
# WA INBOX HELPERS
# ---------------------------------------------------------------------------
INBOX_RETENTION_DAYS = 14


def _wa_prune_old_messages(enquiry_id: str):
    cutoff = datetime.utcnow() - timedelta(days=INBOX_RETENTION_DAYS)
    wa_inbox_col.update_one(
        {"enquiry_id": enquiry_id},
        {"$pull": {"messages": {"ts": {"$lt": cutoff}}}}
    )


def _normalise_phone(raw: str) -> str:
    digits = "".join(filter(str.isdigit, str(raw or "").split("@")[0]))
    if len(digits) == 10:
        return "91" + digits
    return digits


def _wa_append_message(enquiry_id: str, direction: str, body: str,
                        phone: str = "", push_name: str = "", message_id: str = ""):
    now        = datetime.utcnow()
    norm_phone = _normalise_phone(phone)
    msg = {
        "direction":  direction,
        "body":       body,
        "ts":         now,
        "message_id": message_id,
    }
    wa_inbox_col.update_one(
        {"enquiry_id": enquiry_id},
        {
            "$push":        {"messages": msg},
            "$set":         {
                "enquiry_id":   enquiry_id,
                "phone":        norm_phone or None,
                "push_name":    push_name or None,
                "last_updated": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True
    )
    _wa_prune_old_messages(enquiry_id)


# ---------------------------------------------------------------------------
# BULK WHATSAPP
# ---------------------------------------------------------------------------

def _send_bulk_wa_worker(targets, message, batch_size=10):
    """
    Sends WhatsApp messages in batches with random gaps.
    targets = list of {"phone": "...", "name": "..."}
    """
    total   = len(targets)
    sent    = 0
    failed  = 0
    batches = [targets[i:i+batch_size] for i in range(0, total, batch_size)]
    gaps    = [5*60, 7*60, 2*60, 4*60, 3*60]   # seconds between batches

    for b_idx, batch in enumerate(batches):
        for t in batch:
            phone = "".join(filter(str.isdigit, str(t.get("phone", ""))))
            if len(phone) == 10:
                phone = "91" + phone
            if not phone:
                failed += 1
                continue
            try:
                personalised = message.replace("{name}", t.get("name", "there"))
                ok = _safe_send_whatsapp(phone, personalised, context="bulk_wa")
                if ok:
                    sent += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                print(f"[BulkWA] Failed {phone}: {e}")
        if b_idx < len(batches) - 1:
            gap = gaps[b_idx % len(gaps)] + random.randint(-30, 30)
            gap = max(60, gap)
            print(f"[BulkWA] Batch {b_idx+1} done ({sent} sent, {failed} fail). Sleeping {gap}s…")
            time.sleep(gap)

    print(f"[BulkWA] All done. Sent={sent} Failed={failed}")




# ---------------------------------------------------------------------------
# PLAN ALTERNATE ITEM HELPER
# ---------------------------------------------------------------------------
def _get_alternate_item_for_date(plan, target_date):
    """
    Returns the alternate item label for a given date using a fixed epoch.
    alternate_items = ["Fruit", "Sprouts"] → alternates daily from 2024-01-01.
    Supports any number of items (2, 3, etc.)
    In the plan admin UI, enter them comma-separated:
      e.g.  "Fruit, Sprouts"  or  "Red Fruit, Green Fruit, Sprouts"
    """
    alt_items = plan.get("alternate_items", [])
    if not alt_items or len(alt_items) < 2:
        return None
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)
    epoch = date(2024, 1, 1)
    delta = (target_date - epoch).days
    idx   = delta % len(alt_items)
    return alt_items[idx]

# ---------------------------------------------------------------------------
# PER-CUSTOMER ALTERNATE ITEM HELPER
# ---------------------------------------------------------------------------
def _get_customer_item_for_date(customer, target_date):
    """
    Returns today's item for a customer based on their personal alt_options sequence.
    alt_options is a list like ["Fruits","Fruits","Sprouts"] — meaning day1=Fruits,
    day2=Fruits, day3=Sprouts, day4=Fruits, day5=Fruits, day6=Sprouts, ...
    Falls back to plan-level alternate_items if customer has none set.
    Returns None if neither is set.
    """
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)

    alt_options = customer.get("alt_options", [])

    if not alt_options:
        # Fall back to plan-level
        if customer.get("plan_id"):
            plan = plans_col.find_one({"_id": ObjectId(customer["plan_id"])})
            if plan:
                return _get_alternate_item_for_date(plan, target_date)
        return None

    epoch = date(2024, 1, 1)
    delta = (target_date - epoch).days
    idx   = delta % len(alt_options)
    return alt_options[idx]


# ---------------------------------------------------------------------------
# MANUAL BILLING HELPER (NEW)
# ---------------------------------------------------------------------------
def _send_manual_bill_whatsapp(phone, message_text):
    phone = "".join(filter(str.isdigit, str(phone or "")))
    if len(phone) == 10:
        phone = "91" + phone
    if not phone:
        return False
    return _safe_send_whatsapp(phone, message_text, context="manual_bill_helper")


def _build_bill_boxes(bill):
    """
    Replays a SAVED manual bill's own delivered_dates/holiday_dates into a
    day-by-day box grid — this is what lets a closed billing period show
    its own frozen history instead of being recalculated against
    deliveries_col (which has already moved on to the next cycle).
    """
    period_start = bill.get("period_start", "")
    period_end   = bill.get("display_period_end") or bill.get("period_end", "")
    if not period_start or not period_end:
        return []
    delivered = set(bill.get("delivered_dates", []))
    holidays  = set(bill.get("holiday_dates", []))
    try:
        current = datetime.strptime(period_start, "%Y-%m-%d").date()
        end     = datetime.strptime(period_end,   "%Y-%m-%d").date()
    except Exception:
        return []
    boxes = []
    while current <= end:
        d_str = current.strftime("%Y-%m-%d")
        if d_str in delivered:
            status = "delivered"
        elif d_str in holidays:
            status = "holiday"
        else:
            status = "upcoming"   # unmarked day inside a closed bill — neutral
        boxes.append({"date": d_str, "day": current.strftime("%A")[:3], "status": status})
        current += timedelta(days=1)
    return boxes


def _serialize_manual_bill(bill):
    return {
        "id":                 str(bill["_id"]),
        "bill_month_label":   bill.get("bill_month_label", ""),
        "period_start":       bill.get("period_start", ""),
        "period_end":         bill.get("period_end", ""),
        "display_period_end": bill.get("display_period_end") or bill.get("period_end", ""),
        "total_days":         bill.get("total_days", 0),
        "leave_days":         bill.get("leave_days", 0),
        "working_days":       bill.get("working_days", 0),
        "rate_per_day":       bill.get("rate_per_day", 0),
        "subtotal":           bill.get("subtotal", 0),
        "discount":           bill.get("discount", 0),
        "total":              bill.get("total", 0),
        "sent":               bill.get("sent", False),
        "created_at":         bill["created_at"].strftime("%Y-%m-%d %H:%M") if isinstance(bill.get("created_at"), datetime) else "",
        "created_by_name":    bill.get("created_by_name", ""),
        "boxes":              _build_bill_boxes(bill),
    }


# ---------------------------------------------------------------------------
# PAYMENT REMINDER HELPERS (NEW)
# ---------------------------------------------------------------------------
PAYMENT_REMINDER_OFFSETS = [0, 1, 2]   # same day, next day, 2 days later

def _send_payment_reminder_whatsapp(customer, offset_day):
    phone = "".join(filter(str.isdigit, str(customer.get("phone", ""))))
    if len(phone) == 10:
        phone = "91" + phone
    if not phone:
        return False
    name = customer.get("name", "")
    plan = plans_col.find_one({"_id": ObjectId(customer["plan_id"])}) if customer.get("plan_id") else None
    plan_name = plan.get("name", "") if plan else customer.get("sample", "your plan")
    if offset_day == 0:
        line = "Your payment for this subscription is still pending."
    elif offset_day == 1:
        line = "Just a gentle reminder — your payment is still pending."
    else:
        line = "This is a final reminder — your payment has been pending for a couple of days now."
    msg = (
        f"🍓 Fruit Delights – Payment Reminder\n\n"
        f"Hello {name},\n\n"
        f"{line}\n"
        f"Plan: {plan_name}\n\n"
        f"Please complete your payment at the earliest to avoid any interruption in delivery.\n\n"
        f"If you've already paid, please ignore this message. 🙏"
    )
    return _safe_send_whatsapp(phone, msg, context="payment_reminder")


def _run_payment_reminders_job():
    """
    For every customer still tagged 'is_new' with pending payment, send a
    reminder on the same day they were tagged (offset 0), the next day
    (offset 1), and two days later (offset 2). Each offset is sent at most
    once — tracked via payment_reminder_days_sent on the user document.
    """
    today = date.today()
    customers = users_col.find({
        "role":           "customer",
        "is_new":         True,
        "payment_status": "pending",
        "new_tagged_at":  {"$exists": True},
    })
    sent_count = 0
    for cust in customers:
        tagged_at = cust.get("new_tagged_at")
        if not isinstance(tagged_at, datetime):
            continue
        days_since = (today - tagged_at.date()).days
        if days_since not in PAYMENT_REMINDER_OFFSETS:
            continue
        if days_since in cust.get("payment_reminder_days_sent", []):
            continue
        if _send_payment_reminder_whatsapp(cust, days_since):
            users_col.update_one(
                {"_id": cust["_id"]},
                {"$addToSet": {"payment_reminder_days_sent": days_since}}
            )
            sent_count += 1
    print(f"[PaymentReminders] Sent {sent_count} reminder(s) for {today}")
    return sent_count


# ---------------------------------------------------------------------------
# ADMIN BOXES SUMMARY (WhatsApp) — NEW
# ---------------------------------------------------------------------------
def _build_boxes_summary_text(target_date):
    target_str = target_date.strftime("%Y-%m-%d")
    plans = list(plans_col.find())
    lines = [f"📦 Boxes to Prepare — {target_date.strftime('%d %b %Y (%A)')}\n"]
    grand_total = 0
    for plan in plans:
        active_customers = list(users_col.find({
            "role": "customer", "status": "active", "plan_id": plan["_id"]
        }))
        count = sum(
            1 for c in active_customers
            if _day_schedulable_for_plan(plan, target_date, c.get("off_days", []), set(c.get("holidays", [])))
        )
        if count == 0:
            continue
        grand_total += count
        lines.append(f"• {plan.get('name','')}: {count} box(es)")

    sample_customers = list(users_col.find({
        "role": "customer", "status": "active", "sample": {"$exists": True, "$nin": [None, ""]}
    }))
    sample_total = 0
    day_name = target_date.strftime("%A")
    for c in sample_customers:
        if day_name in c.get("off_days", []) or target_str in c.get("holidays", []):
            continue
        sample_total += 1
    if sample_total:
        lines.append(f"• Sample boxes: {sample_total} box(es)")
        grand_total += sample_total

    lines.append(f"\n🎯 Grand Total: {grand_total} box(es)")
    return "\n".join(lines)


def _send_admin_boxes_tomorrow_summary():
    numbers = _get_admin_whatsapp_numbers()
    if not numbers:
        print("[BoxesSummary] No ADMIN_WHATSAPP_NUMBER(S) set — skipping.")
        return False
    tomorrow = (datetime.now() + timedelta(days=1)).date()
    text = _build_boxes_summary_text(tomorrow)
    all_ok = True
    for num in numbers:
        ok = _safe_send_whatsapp(num, text, context="boxes_tomorrow_summary")
        all_ok = all_ok and ok
    return all_ok


# ---------------------------------------------------------------------------
# ROUTES – core
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    if "user_id" in session:
        role = session.get("role")
        if role in ["admin", "manager", "subadmin"]:
            return redirect(url_for("dashboard_admin"))
        elif role == "employee":
            return redirect(url_for("dashboard_employee"))
        elif role == "customer":
            return redirect(url_for("dashboard_customer"))
    return render_template("index.html")


@app.route("/sw.js")
def sw():
    return send_from_directory("static", "sw.js")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email    = request.form.get("email")
        password = request.form.get("password")
        user     = users_col.find_one({"email": email})
        if user and check_password_hash(user["password"], password):
            if user.get("role") == "subadmin":
                fr = franchises_col.find_one({"_id": user.get("franchise_id")})
                if not fr or fr.get("status") != "active":
                    flash("Your franchise account is pending approval. Contact HQ support.", "danger")
                    return render_template("login.html")
            session["user_id"] = str(user["_id"])
            session["role"]    = user["role"]
            session["name"]    = user["name"]
            return redirect(url_for("home"))
        flash("Invalid credentials", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# FORGOT PASSWORD
# ---------------------------------------------------------------------------
@app.route("/api/forgot-password", methods=["POST"])
def forgot_password():
    data  = request.get_json(silent=True) or {}
    phone = "".join(filter(str.isdigit, str(data.get("phone", "")).strip()))
    if not phone:
        return jsonify({"success": False, "message": "Phone number is required."}), 400
    user = users_col.find_one({"phone": phone})
    if not user and phone.startswith("91") and len(phone) == 12:
        user = users_col.find_one({"phone": phone[2:]})
    if not user and len(phone) == 10:
        user = users_col.find_one({"phone": "91" + phone})
    if not user:
        return jsonify({"success": True, "message": "If this number is registered, you'll receive your credentials on WhatsApp shortly."})
    new_password = secrets.token_urlsafe(8)
    users_col.update_one({"_id": user["_id"]}, {"$set": {"password": generate_password_hash(new_password)}})
    wa_phone = "".join(filter(str.isdigit, str(user.get("phone", ""))))
    if len(wa_phone) == 10:
        wa_phone = "91" + wa_phone
    login_id = user.get("email", "")
    name     = user.get("name", "")
    msg = (
        f"🍓 Fruit Delights – Login Details\n\n"
        f"Hello {name},\n\n"
        f"Your login credentials:\n\n"
        f"User ID: {login_id}\n"
        f"Password: {new_password}\n\n"
        f"Login here:\nhttps://fruitedelights.com/login\n\n"
        f"If you did not request this, please contact support."
    )
    _safe_send_whatsapp(wa_phone, msg, context="forgot_password")
    return jsonify({"success": True, "message": "New credentials sent to your WhatsApp."})


# ---------------------------------------------------------------------------
# ADMIN – dashboard & analytics
# ---------------------------------------------------------------------------
@app.route("/admin")
@login_required
@role_required("admin", "manager")
def dashboard_admin():
    now   = datetime.now()
    today = now.strftime("%Y-%m-%d")
    tf    = _manager_team_filter()
    ff    = _franchise_filter()

    cust_q = {"role": "customer"}
    if tf:
        cust_q.update(tf)

    scoped_team_ids = _scoped_team_ids()
    delivery_scope  = {"team_id": {"$in": scoped_team_ids}}

    stats = {
        "total_customers":    users_col.count_documents(cust_q),
        "total_employees":    users_col.count_documents({**{"role": "employee"}, **tf}),
        "pending_deliveries": deliveries_col.count_documents({**delivery_scope, "status": "pending",           "date": today}),
        "delivered_today":    deliveries_col.count_documents({**delivery_scope, "status": "delivered",         "date": today}),
        "on_holiday_today":   deliveries_col.count_documents({**delivery_scope, "status": "cancelled_holiday", "date": today}),
        "active_teams":       teams_col.count_documents(ff),
    }

    if session.get("role") == "manager":
        user = users_col.find_one({"_id": ObjectId(session["user_id"])})
        tids = (user.get("team_ids") or ([user["team_id"]] if user and user.get("team_id") else [])) if user else []
        teams = list(teams_col.find({"_id": {"$in": tids}})) if tids else []
    else:
        teams = list(teams_col.find(ff))

    plans = list(plans_col.find(ff))
    for t in teams: t["_id"] = str(t["_id"])
    for p in plans: p["_id"] = str(p["_id"])

    return render_template("dashboard_admin.html",
                           stats=stats, teams=teams, plans=plans,
                           active_page="home")



@app.route("/api/admin/employees")
@login_required
@role_required("admin", "manager")
def api_employees():
    tf        = _manager_team_filter()
    query     = {"role": "employee", **tf}
    employees = list(users_col.find(query))
    teams     = list(teams_col.find())
    team_map  = {str(t["_id"]): t for t in teams}
    result = []
    for e in employees:
        team = team_map.get(str(e.get("team_id", "")), {})
        result.append({
            "id":        str(e["_id"]),
            "name":      e.get("name", ""),
            "email":     e.get("email", ""),
            "phone":     e.get("phone", "—"),
            "team_id":   str(e.get("team_id", "")),
            "team_name": team.get("name", "—"),
            "team_city": team.get("city", ""),
        })
    return jsonify(result)


@app.route("/api/admin/customers")
@login_required
@role_required("admin", "manager")
def api_customers():
    tf        = _manager_team_filter()
    query     = {"role": "customer", **tf}
    customers = list(users_col.find(query))
    teams     = list(teams_col.find())
    plans     = list(plans_col.find())
    team_map  = {str(t["_id"]): t for t in teams}
    plan_map  = {str(p["_id"]): p for p in plans}

    today     = date.today()
    tomorrow  = today + timedelta(days=1)
    today_str = today.strftime("%Y-%m-%d")
    tmrw_str  = tomorrow.strftime("%Y-%m-%d")
    today_dow = today.strftime("%A")
    tmrw_dow  = tomorrow.strftime("%A")

    result = []
    for c in customers:
        team = team_map.get(str(c.get("team_id", "")), {})
        plan = plan_map.get(str(c.get("plan_id", "")), {})
        start_date = c.get("start_date")
        if isinstance(start_date, datetime):
            start_date = start_date.strftime("%Y-%m-%d")

        holidays = c.get("holidays", [])
        off_days = c.get("off_days", [])
        holiday_today    = (today_str in holidays) or (today_dow in off_days)
        holiday_tomorrow = (tmrw_str in holidays) or (tmrw_dow in off_days)

        result.append({
            "id":               str(c["_id"]),
            "name":             c.get("name", ""),
            "email":            c.get("email", ""),
            "phone":            c.get("phone", "—"),
            "address":          c.get("address", ""),
            "preferred_time":   c.get("preferred_time", ""),
            "start_date":       start_date or "",
            "team_id":          str(c.get("team_id", "")),
            "team_name":        team.get("name", "—"),
            "team_city":        team.get("city", ""),
            "team_area":        team.get("area", ""),
            "plan_id":          str(c.get("plan_id", "")),
            "plan_name":        plan.get("name", "—"),
            "plan_price":       plan.get("price_per_month") or plan.get("price_per_day") or 0,
            "plan_tax":         plan.get("tax_percent", 0),
            "plan_duration":    plan.get("duration_days", 0),
            "off_days":         c.get("off_days", []),
            "holidays":         c.get("holidays", []),
            "discount_percent": float(c.get("discount_percent", 0) or 0),
            "discount_amount":  float(c.get("discount_amount",  0) or 0),
            "status":           c.get("status", "active"),
            "source":           c.get("source", ""),
            "payment_status":          c.get("payment_status", "pending"),
            "payment_partial_amount":  float(c.get("payment_partial_amount", 0) or 0),
            "is_new":           bool(c.get("is_new", False)),
            "holiday_today":    holiday_today,
            "holiday_tomorrow": holiday_tomorrow,
            "created_at":       (
                c["created_at"].strftime("%Y-%m-%d")
                if isinstance(c.get("created_at"), datetime) else ""
            ),
        })
    return jsonify(result)

# ---------------------------------------------------------------------------
# ADMIN – enquiries DATA API
# ---------------------------------------------------------------------------
@app.route("/api/admin/enquiries_data")
@login_required
@role_required("admin", "manager")
def api_enquiries_data():
    enquiries = list(enquiries_col.find({"tag": "form_enquiry"}).sort("created_at", -1))
    enq_ids_with_msgs = set(
        str(doc["enquiry_id"])
        for doc in wa_inbox_col.find({}, {"enquiry_id": 1})
    )
    result = []
    for e in enquiries:
        enq_id = str(e["_id"])
        result.append({
            "id":             enq_id,
            "name":           e.get("name", ""),
            "phone":          e.get("phone", ""),
            "address":        e.get("address", ""),
            "plan":           e.get("plan", ""),
            "delivery_time":  e.get("delivery_time", ""),
            "start_date":     e.get("start_date", ""),
            "status":         e.get("status", "pending"),
            "payment_method": e.get("payment_method", "upi"),
            "created_at":     e["created_at"].isoformat() if e.get("created_at") else None,
            "whatsapp_id":    e.get("whatsapp_id", ""),
            "has_inbox":      enq_id in enq_ids_with_msgs,
        })
    return jsonify(result)


# ---------------------------------------------------------------------------
# ADMIN – analytics API
# ---------------------------------------------------------------------------
@app.route("/api/admin/analytics")
@login_required
@role_required("admin", "manager")
def admin_analytics():
    range_days = request.args.get("range", "7")
    try:
        days = int(range_days)
    except ValueError:
        days = 7

    scoped_team_ids = _scoped_team_ids()
    team_scope = {"team_id": {"$in": scoped_team_ids}}

    results = []
    for i in range(days - 1, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        results.append({
            "date":      d,
            "delivered": deliveries_col.count_documents({**team_scope, "status": "delivered",         "date": d}),
            "pending":   deliveries_col.count_documents({**team_scope, "status": "pending",           "date": d}),
            "holiday":   deliveries_col.count_documents({**team_scope, "status": "cancelled_holiday", "date": d}),
        })

    today = datetime.now().strftime("%Y-%m-%d")
    teams = list(teams_col.find({"_id": {"$in": scoped_team_ids}}))
    area_data = []
    for t in teams:
        cnt = deliveries_col.count_documents({"team_id": t["_id"], "status": "pending", "date": today})
        area_data.append({"area": t.get("area", t["name"]), "pending": cnt})

    return jsonify({"timeseries": results, "area_pending": area_data})

@app.route("/api/admin/enquiries/edit/<enquiry_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def edit_enquiry(enquiry_id):
    data = request.get_json(silent=True) or {}
    allowed = ["name", "phone", "address", "plan", "delivery_time", "start_date", "payment_method", "status"]
    update = {k: data[k] for k in allowed if k in data}
    if not update:
        return jsonify({"success": False, "message": "Nothing to update."}), 400
    enquiries_col.update_one({"_id": ObjectId(enquiry_id)}, {"$set": update})
    return jsonify({"success": True, "message": "Enquiry updated."})


# ---------------------------------------------------------------------------
# ADMIN – plans
# ---------------------------------------------------------------------------


@app.route("/admin/plans")
@login_required
@role_required("admin", "manager")
def list_plans():
    plans = list(plans_col.find(_franchise_filter()))
    return render_template("plans.html", plans=plans, active_page="plans")


@app.route("/admin/plans/add", methods=["POST"])
@login_required
@role_required("admin", "manager")
def add_plan():
    alt_raw  = request.form.get("alternate_items", "").strip()
    alt_list = [x.strip() for x in alt_raw.split(",") if x.strip()] if alt_raw else []
    plans_col.insert_one({
        "name":            request.form.get("name"),
        "description":     request.form.get("description"),
        "price_per_month": float(request.form.get("price_per_month", 0)),
        "duration_days":   int(request.form.get("duration_days", 0)),
        "tax_percent":     float(request.form.get("tax_percent", 0)),
        "alternate_items": alt_list,
        "franchise_id":    _current_franchise_id(),
        "created_at":      datetime.now()
    })
    flash(f"✅ Plan '{request.form.get('name')}' created successfully.", "success")
    return redirect(url_for("dashboard_admin"))


@app.route("/admin/plans/edit/<plan_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def edit_plan(plan_id):
    alt_raw  = request.form.get("alternate_items", "").strip()
    alt_list = [x.strip() for x in alt_raw.split(",") if x.strip()] if alt_raw else []
    plans_col.update_one({"_id": ObjectId(plan_id)}, {"$set": {
        "name":            request.form.get("name"),
        "description":     request.form.get("description"),
        "price_per_month": float(request.form.get("price_per_month", 0)),
        "duration_days":   int(request.form.get("duration_days", 0)),
        "tax_percent":     float(request.form.get("tax_percent", 0)),
        "alternate_items": alt_list,
    }})
    flash("Plan updated.", "success")
    return redirect(url_for("dashboard_admin"))

@app.route("/admin/plans/delete/<plan_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def delete_plan(plan_id):
    plans_col.delete_one({"_id": ObjectId(plan_id)})
    flash("Plan deleted.", "warning")
    return redirect(url_for("dashboard_admin"))


# ---------------------------------------------------------------------------
# ADMIN – teams
# ---------------------------------------------------------------------------
@app.route("/admin/teams")
@login_required
@role_required("admin", "manager")
def list_teams():
    city_filter = request.args.get("city", "")
    query       = dict(_franchise_filter())
    if city_filter:
        query["city"] = {"$regex": city_filter, "$options": "i"}
    teams  = list(teams_col.find(query))
    for t in teams:
        t["employee_count"] = users_col.count_documents({"role": "employee", "team_id": t["_id"]})
    cities = teams_col.distinct("city", _franchise_filter())
    return render_template("teams.html", teams=teams, cities=cities,
                           city_filter=city_filter, active_page="teams")


@app.route("/admin/teams/add", methods=["POST"])
@login_required
@role_required("admin", "manager")
def add_team():
    teams_col.insert_one({
        "name":         request.form.get("name"),
        "area":         request.form.get("area"),
        "city":         request.form.get("city"),
        "franchise_id": _current_franchise_id(),
        "created_at":   datetime.now()
    })
    flash(f"✅ Team '{request.form.get('name')}' added successfully.", "success")
    return redirect(url_for("dashboard_admin"))

@app.route("/admin/teams/edit/<team_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def edit_team(team_id):
    teams_col.update_one({"_id": ObjectId(team_id)}, {"$set": {
        "name": request.form.get("name"),
        "area": request.form.get("area"),
        "city": request.form.get("city"),
    }})
    flash("Team updated.", "success")
    return redirect(url_for("dashboard_admin"))


@app.route("/admin/teams/delete/<team_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def delete_team(team_id):
    teams_col.delete_one({"_id": ObjectId(team_id)})
    flash("Team deleted.", "warning")
    return redirect(url_for("dashboard_admin"))


# ---------------------------------------------------------------------------
# ADMIN – employees
# ---------------------------------------------------------------------------
@app.route("/admin/employees")
@login_required
@role_required("admin", "manager")
def list_employees():
    team_filter = request.args.get("team_id", "")
    city_filter = request.args.get("city", "")
    query       = {"role": "employee"}
    if team_filter:
        query["team_id"] = ObjectId(team_filter)
    employees = list(users_col.find(query))
    teams     = list(teams_col.find())
    team_map  = {str(t["_id"]): t for t in teams}
    if city_filter:
        city_team_ids = [t["_id"] for t in teams if t.get("city", "").lower() == city_filter.lower()]
        employees     = [e for e in employees if e.get("team_id") in city_team_ids]
    cities = teams_col.distinct("city")
    return render_template("employees.html",
                           employees=employees, teams=teams, team_map=team_map,
                           cities=cities, team_filter=team_filter, city_filter=city_filter,
                           active_page="employees")


@app.route("/admin/employees/reset-credentials/<employee_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def reset_employee_credentials(employee_id):
    employee = users_col.find_one({"_id": ObjectId(employee_id), "role": "employee"})
    if not employee:
        return jsonify({"success": False, "message": "Employee not found."}), 404
    new_password = secrets.token_urlsafe(8)
    users_col.update_one({"_id": ObjectId(employee_id)},
                         {"$set": {"password": generate_password_hash(new_password)}})
    phone = "".join(filter(str.isdigit, str(employee.get("phone", ""))))
    if len(phone) == 10:
        phone = "91" + phone
    name     = employee.get("name", "")
    login_id = employee.get("email", "")
    msg = (
        f"🍓 Fruit Delights – Login Credentials Reset\n\n"
        f"Hello {name},\n\n"
        f"Your login credentials have been reset.\n\n"
        f"User ID: {login_id}\n"
        f"New Password: {new_password}\n\n"
        f"Login:\nhttps://fruitedelights.com/login\n\n"
        f"If you did not request this, contact your manager immediately."
    )
    wa_sent = False
    if phone:
        wa_sent = _safe_send_whatsapp(phone, msg, context="reset_employee")
    return jsonify({
        "success":     True,
        "wa_sent":     wa_sent,
        "name":        name,
        "login_id":    login_id,
        "new_password": new_password,
        "message": f"Credentials reset for {name}." + (" Sent on WhatsApp." if wa_sent else " WhatsApp delivery failed."),
    })


# ---------------------------------------------------------------------------
# ADMIN – reset customer credentials (NEW: password reset button)
# ---------------------------------------------------------------------------
@app.route("/admin/customers/reset-credentials/<customer_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def reset_customer_credentials(customer_id):
    customer = users_col.find_one({"_id": ObjectId(customer_id), "role": "customer"})
    if not customer:
        return jsonify({"success": False, "message": "Customer not found."}), 404
    new_password = secrets.token_urlsafe(8)
    users_col.update_one({"_id": ObjectId(customer_id)},
                         {"$set": {"password": generate_password_hash(new_password)}})
    phone = "".join(filter(str.isdigit, str(customer.get("phone", ""))))
    if len(phone) == 10:
        phone = "91" + phone
    name, login_id = customer.get("name", ""), customer.get("email", "")
    msg = (
        f"🍓 Fruit Delights – Password Reset\n\n"
        f"Hello {name},\n\nYour password has been reset.\n\n"
        f"Login ID: {login_id}\nNew Password: {new_password}\n\n"
        f"Login:\nhttps://fruitedelights.com/login\n\n"
        f"If you did not request this, contact support."
    )
    wa_sent = _safe_send_whatsapp(phone, msg, context="reset_customer") if phone else False
    return jsonify({
        "success": True, "wa_sent": wa_sent, "name": name,
        "login_id": login_id, "new_password": new_password,
        "message": f"Password reset for {name}." + (" Sent on WhatsApp." if wa_sent else " WhatsApp delivery failed."),
    })


# ---------------------------------------------------------------------------
# ADMIN – add / edit / delete users
# ---------------------------------------------------------------------------

@app.route("/admin/users/add", methods=["POST"])
@login_required
@role_required("admin", "manager")
def add_user():
    role  = request.form.get("role")
    phone = request.form.get("phone", "").strip()
    name  = request.form.get("name", "").strip()
    if role == "customer":
        email = _resolve_customer_email(phone, request.form.get("email", ""))
    else:
        email = request.form.get("email", "").strip()
    if email:
        existing = users_col.find_one({"email": email})
        if existing:
            flash(
                f"⚠️ User ID '{email}' is already taken by '{existing.get('name', 'another user')}'. "
                f"Please use a different User ID.",
                "danger"
            )
            return redirect(url_for("dashboard_admin"))
    raw_password = secrets.token_urlsafe(8)
    user_data = {
        "name":         name,
        "email":        email,
        "phone":        phone,
        "role":         role,
        "franchise_id": _current_franchise_id(),
        "password":     generate_password_hash(raw_password),
        "created_at":   datetime.now()
    }
    if role in ["employee", "customer"]:
        tid = request.form.get("team_id")
        if tid:
            user_data["team_id"] = ObjectId(tid)
    if role == "customer":
        plan_name      = request.form.get("plan_name")
        plan           = plans_col.find_one({"name": plan_name})
        start_date_str = request.form.get("start_date", "").strip()
        start_date = None
        if start_date_str:
            try:
                start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            except Exception:
                pass
            user_data.update({
            "plan_id":          plan["_id"] if plan else None,
            "address":          request.form.get("address"),
            "preferred_time":   request.form.get("preferred_time"),
            "off_days":         request.form.getlist("off_days"),
            "holidays":         [],
            "start_date":       start_date or datetime.now(),
            "discount_percent": float(request.form.get("discount_percent", 0) or 0),
            "discount_amount":  float(request.form.get("discount_amount",  0) or 0),
            "status":           request.form.get("customer_status", "active"),
            "payment_status":          "pending",
            "payment_partial_amount":  0,
            # NEW
            "is_new":        True,
            "new_tagged_at": datetime.now(),
            "payment_reminder_days_sent": [],
        })
    users_col.insert_one(user_data)
    send_credentials_whatsapp(email, raw_password, name, phone)
    flash(f"✅ {role.capitalize()} '{name}' added successfully.", "success")
    return redirect(url_for("dashboard_admin"))


@app.route("/admin/users/edit/<user_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def edit_user(user_id):
    role  = request.form.get("role")
    phone = request.form.get("phone", "").strip()

    # Always preserve existing email — never blank it accidentally
    existing_user  = users_col.find_one({"_id": ObjectId(user_id)}, {"email": 1, "payment_status": 1})
    existing_email = (existing_user or {}).get("email", "")
    existing_payment_status = (existing_user or {}).get("payment_status", "pending")
    form_email     = request.form.get("email", "").strip()
    email          = form_email if form_email else existing_email

    update = {
        "name":  request.form.get("name"),
        "email": email,
        "phone": phone,
    }

    payment_status_changed = False

    if role in ["employee", "customer"]:
        tid = request.form.get("team_id")
        if tid:
            update["team_id"] = ObjectId(tid)
    if role == "customer":
        plan_name      = request.form.get("plan_name")
        plan           = plans_col.find_one({"name": plan_name})
        start_date_str = request.form.get("start_date", "").strip()
        start_date = None
        if start_date_str:
            try:
                start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            except Exception:
                pass

        # Parse per-customer alt options  e.g. "Fruits, Fruits, Sprouts"
        alt_raw  = request.form.get("alt_options", "").strip()
        alt_list = [x.strip() for x in alt_raw.split(",") if x.strip()] if alt_raw else []

        # Total delivered boxes override
        tdb = request.form.get("total_delivered_boxes", "").strip()
        total_delivered_boxes = int(tdb) if tdb.isdigit() else None

        # NEW: payment status
        payment_status   = request.form.get("payment_status", "pending")
        partial_amt_raw  = request.form.get("payment_partial_amount", "").strip()
        payment_partial_amount = float(partial_amt_raw) if (payment_status == "partial" and partial_amt_raw) else 0

        payment_status_changed = payment_status != existing_payment_status

        update.update({
            "plan_id":               plan["_id"] if plan else None,
            "address":               request.form.get("address"),
            "preferred_time":        request.form.get("preferred_time"),
            "off_days":              request.form.getlist("off_days"),
            "start_date":            start_date,
            "discount_percent":      float(request.form.get("discount_percent", 0) or 0),
            "discount_amount":       float(request.form.get("discount_amount",  0) or 0),
            "status":                request.form.get("customer_status", "active"),
            "alt_options":           alt_list,
            "payment_status":            payment_status,
            "payment_partial_amount":    payment_partial_amount,
        })
        if total_delivered_boxes is not None:
            update["total_delivered_boxes"] = total_delivered_boxes

    # NEW: whenever admin changes a customer's payment status, clear the
    # "New" tag (stop payment reminders + New badge) and clear the "Sample"
    # tag (they become an ordinary customer).
    unset_fields = {}
    if role == "customer" and payment_status_changed:
        unset_fields["sample"] = ""
        update["is_new"] = False

    mongo_update = {"$set": update}
    if unset_fields:
        mongo_update["$unset"] = unset_fields
    users_col.update_one({"_id": ObjectId(user_id)}, mongo_update)
    flash("User updated.", "success")
    next_page = request.form.get("next", "dashboard_admin")
    return redirect(url_for(next_page))


@app.route("/admin/users/delete/<user_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def delete_user(user_id):
    users_col.delete_one({"_id": ObjectId(user_id)})
    flash("User removed.", "warning")
    next_page = request.form.get("next", "dashboard_admin")
    return redirect(url_for(next_page))


# ---------------------------------------------------------------------------
# API – Get single user details (for edit-form auto-populate)
# ---------------------------------------------------------------------------
@app.route("/api/admin/user/<user_id>")
@login_required
@role_required("admin", "manager")
def api_get_user(user_id):
    """
    Returns full user data so the edit form can auto-populate every field,
    including the email (login ID) field.
    Call this when opening the edit modal: GET /api/admin/user/<id>
    """
    try:
        user = users_col.find_one({"_id": ObjectId(user_id)})
    except Exception:
        return jsonify({"success": False, "message": "Invalid user ID"}), 400
    if not user:
        return jsonify({"success": False, "message": "User not found"}), 404

    start_date = user.get("start_date")
    if isinstance(start_date, datetime):
        start_date = start_date.strftime("%Y-%m-%d")

    return jsonify({
        "success":               True,
        "id":                    str(user["_id"]),
        "name":                  user.get("name", ""),
        "email":                 user.get("email", ""),
        "phone":                 user.get("phone", ""),
        "role":                  user.get("role", ""),
        "team_id":               str(user.get("team_id", "")),
        "plan_id":               str(user.get("plan_id", "")),
        "address":               user.get("address", ""),
        "preferred_time":        user.get("preferred_time", ""),
        "off_days":              user.get("off_days", []),
        "start_date":            start_date or "",
        "discount_percent":      float(user.get("discount_percent", 0) or 0),
        "discount_amount":       float(user.get("discount_amount",  0) or 0),
        "status":                user.get("status", "active"),
        "alt_options":           user.get("alt_options", []),
        "total_delivered_boxes": user.get("total_delivered_boxes", ""),
        # NEW: payment status fields
        "payment_status":         user.get("payment_status", "pending"),
        "payment_partial_amount": float(user.get("payment_partial_amount", 0) or 0),
    })

# ---------------------------------------------------------------------------
# ADMIN – customers list
# ---------------------------------------------------------------------------
@app.route("/admin/customers")
@login_required
@role_required("admin", "manager")
def list_customers():
    team_filter = request.args.get("team_id", "")
    city_filter = request.args.get("city", "")
    query       = {"role": "customer"}
    if team_filter:
        query["team_id"] = ObjectId(team_filter)
    customers = list(users_col.find(query))
    teams     = list(teams_col.find())
    team_map  = {str(t["_id"]): t for t in teams}
    plan_map  = {str(p["_id"]): p for p in plans_col.find()}
    if city_filter:
        city_team_ids = [t["_id"] for t in teams if t.get("city", "").lower() == city_filter.lower()]
        customers     = [c for c in customers if c.get("team_id") in city_team_ids]
    cities = teams_col.distinct("city")
    return render_template("customers.html",
                           customers=customers, teams=teams, team_map=team_map,
                           plan_map=plan_map, cities=cities,
                           team_filter=team_filter, city_filter=city_filter,
                           active_page="customers")


@app.route("/admin/customers/<customer_id>")
@login_required
@role_required("admin", "manager")
def customer_detail(customer_id):
    try:
        customer = users_col.find_one({"_id": ObjectId(customer_id)})
    except Exception:
        customer = None
    if not customer:
        flash("Customer not found.", "danger")
        return redirect(url_for("list_customers"))
    customer["discount_percent"] = customer.get("discount_percent", 0) or 0
    customer["discount_amount"]  = customer.get("discount_amount",  0) or 0
    customer["off_days"]         = customer.get("off_days", [])
    customer["status"]           = customer.get("status", "active")
    customer["phone"]            = customer.get("phone", "")
    customer["address"]          = customer.get("address", "")
    customer["preferred_time"]   = customer.get("preferred_time", "")
    now  = datetime.now()
    plan = plans_col.find_one({"_id": ObjectId(customer["plan_id"])}) if customer.get("plan_id") else None
    if plan and int(plan.get("duration_days", 0)) > 0:
        _, period_end = _calculate_invoice_period(customer, plan)
        next_billing  = datetime.combine(period_end + timedelta(days=1), datetime.min.time())
    else:
        next_billing = datetime(now.year, now.month + 1, 1) if now.month < 12 else datetime(now.year + 1, 1, 1)
    current_bill   = generate_monthly_bill(customer, now.month, now.year)
    estimated_bill = estimate_upcoming_bill(customer, now.month, now.year)
    billing_history = []
    for delta in range(1, 4):
        m = now.month - delta
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        billing_history.append({
            "label":  datetime(y, m, 1).strftime("%B %Y"),
            "amount": generate_monthly_bill(customer, m, y)
        })
    teams = list(teams_col.find())
    plans = list(plans_col.find())
    team  = teams_col.find_one({"_id": customer.get("team_id")}) if customer.get("team_id") else None
    thirty_days_ago   = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    recent_deliveries = list(
        deliveries_col.find({
            "customer_id": customer["_id"],
            "date": {"$gte": thirty_days_ago}
        }).sort("date", -1).limit(30)
    )
    month_start = datetime(now.year, now.month, 1).strftime("%Y-%m-%d")
    month_stats = {
        "delivered": deliveries_col.count_documents({
            "customer_id": customer["_id"], "status": "delivered",         "date": {"$gte": month_start}}),
        "pending":   deliveries_col.count_documents({
            "customer_id": customer["_id"], "status": "pending",           "date": {"$gte": month_start}}),
        "holiday":   deliveries_col.count_documents({
            "customer_id": customer["_id"], "status": "cancelled_holiday", "date": {"$gte": month_start}}),
    }
    invoices = list(
        invoices_col.find({"customer_id": customer["_id"]})
        .sort([("year", -1), ("month", -1)])
        .limit(6)
    )
    start_date = customer.get("start_date")
    if isinstance(start_date, datetime):
        start_date_str = start_date.strftime("%Y-%m-%d")
    elif isinstance(start_date, str):
        start_date_str = start_date
    else:
        start_date_str = ""
    member_since     = customer.get("created_at")
    member_since_str = member_since.strftime("%d %b %Y") if isinstance(member_since, datetime) else "—"
    is_sample    = bool(customer.get("sample"))
    sample_label = customer.get("sample", "")
    return render_template(
        "customer_detail.html",
        customer=customer, plan=plan, team=team, plans=plans, teams=teams,
        current_bill=current_bill, estimated_bill=estimated_bill,
        next_billing=next_billing, billing_history=billing_history,
        recent_deliveries=recent_deliveries, month_stats=month_stats,
        invoices=invoices, start_date_str=start_date_str,
        member_since_str=member_since_str, is_sample=is_sample,
        sample_label=sample_label, now=now, active_page="customers"
    )


# ---------------------------------------------------------------------------
# PUBLIC – Enquiry submit
# ---------------------------------------------------------------------------
@app.route("/enquiry/submit", methods=["POST"])
def submit_enquiry():
    name           = request.form.get("fullName", "").strip()
    phone          = request.form.get("mobile", "").strip()
    address        = request.form.get("address", "").strip()
    delivery_time  = request.form.get("deliveryTime", "").strip()
    start_date     = request.form.get("startDate", "").strip()
    plan           = request.form.get("selectedPlan") or request.form.get("planSelect", "")
    payment_method = (request.form.get("payment_method") or "").strip()
    if not name or not phone:
        flash("Name and phone are required.", "danger")
        return redirect(url_for("home") + "#order")
    enquiries_col.insert_one({
        "name":           name,
        "phone":          phone,
        "address":        address,
        "delivery_time":  delivery_time,
        "start_date":     start_date,
        "plan":           plan,
        "payment_method": payment_method,
        "status":         "pending",
        "source":         "web_form",
        "tag":            "form_enquiry",
        "created_at":     datetime.now(),
    })
    flash("Thanks! We'll reach you on WhatsApp shortly. 🍓", "success")
    return redirect(url_for("home") + "#order")


# ---------------------------------------------------------------------------
# ADMIN – List / Approve / Delete enquiries
# ---------------------------------------------------------------------------
@app.route("/admin/enquiries")
@login_required
@role_required("admin", "manager")
def list_enquiries():
    enquiries = list(enquiries_col.find({"tag": "form_enquiry"}).sort("created_at", -1))
    return render_template("enquiries.html", enquiries=enquiries, active_page="enquiries")


@app.route("/admin/enquiries/approve/<enquiry_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def approve_enquiry(enquiry_id):
    enq = enquiries_col.find_one({"_id": ObjectId(enquiry_id)})
    if not enq:
        flash("Enquiry not found.", "danger")
        return redirect(url_for("dashboard_admin"))
    raw_password = secrets.token_urlsafe(8)
    name     = enq.get("name", "customer")
    phone    = enq.get("phone", "unknown")
    enq_plan = (enq.get("plan") or "").strip()
    matched_plan = None
    if enq_plan:
        matched_plan = plans_col.find_one({"name": enq_plan})
        if not matched_plan:
            matched_plan = plans_col.find_one({
                "name": {"$regex": re.escape(enq_plan[:20]), "$options": "i"}
            })
        if not matched_plan:
            first_word = re.split(r'[\s\-–—]', enq_plan)[0].strip()
            if len(first_word) >= 3:
                matched_plan = plans_col.find_one({
                    "name": {"$regex": re.escape(first_word), "$options": "i"}
                })
    if matched_plan:
        plan_id_val = matched_plan["_id"]
        sample_val  = None
        plan_label  = matched_plan["name"]
    else:
        plan_id_val = None
        sample_val  = enq_plan or "Sample"
        plan_label  = enq_plan or "Sample"
    email = _generate_customer_login_id(name)
    start_date = None
    if enq.get("start_date"):
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                start_date = datetime.strptime(enq["start_date"], fmt)
                break
            except Exception:
                continue
    user_data = {
        "name":             name,
        "email":            email,
        "phone":            phone,
        "role":             "customer",
        "franchise_id":     _current_franchise_id(),
        "password":         generate_password_hash(raw_password),
        "address":          enq.get("address"),
        "preferred_time":   enq.get("delivery_time"),
        "plan_id":          plan_id_val,
        "off_days":         [],
        "holidays":         [],
        "start_date":       start_date or datetime.now(),
        "discount_percent": 0,
        "discount_amount":  0,
        "status":           "active",
        "created_at":       datetime.now(),
        "source":           "web_enquiry",
        "payment_status":          "pending",
        "payment_partial_amount":  0,
        # NEW
        "is_new":       True,
        "new_tagged_at": datetime.now(),
        "payment_reminder_days_sent": [],
    }
    if sample_val:
        user_data["sample"] = sample_val
    users_col.insert_one(user_data)
    enquiries_col.delete_one({"_id": ObjectId(enquiry_id)})
    wa_phone = "".join(filter(str.isdigit, str(phone)))
    if len(wa_phone) == 10:
        wa_phone = "91" + wa_phone
    msg = (
        f"🍓 Fruit Delights – Welcome!\n\n"
        f"Hello {name},\n\n"
        f"Your sample box is confirmed! 🎉\n"
        f"Plan: {plan_label}\n\n"
        f"🔑 Login ID: {email}\n"
        f"🔒 Password: {raw_password}\n\n"
        f"👉 Login here:\nhttps://fruitedelights.com/login\n\n"
        f"Thank you for joining us! 🍉"
    )
    _safe_send_whatsapp(wa_phone, msg, context="approve_enquiry")
    sample_note = f" (Sample: {plan_label})" if sample_val else f" (Plan: {plan_label})"
    flash(f"✅ '{name}' approved{sample_note} → Login: {email} sent on WhatsApp.", "success")
    return redirect(url_for("dashboard_admin"))

@app.route("/admin/enquiries/delete/<enquiry_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def delete_enquiry(enquiry_id):
    enquiries_col.delete_one({"_id": ObjectId(enquiry_id)})
    flash("Enquiry removed.", "warning")
    return redirect(url_for("dashboard_admin"))


@app.route("/api/admin/enquiry_count")
@login_required
@role_required("admin", "manager")
def enquiry_count():
    count = enquiries_col.count_documents({"tag": "form_enquiry", "status": "pending"})
    return jsonify({"pending": count})


# ---------------------------------------------------------------------------
# WHATSAPP INBOX – Send message from admin
# ---------------------------------------------------------------------------
@app.route("/api/admin/wa_send", methods=["POST"])
@login_required
@role_required("admin", "manager")
def wa_send():
    data       = request.get_json(silent=True) or {}
    enquiry_id = data.get("enquiry_id", "").strip()
    message    = data.get("message", "").strip()
    if not enquiry_id or not message:
        return jsonify({"success": False, "message": "enquiry_id and message are required."}), 400
    enq = enquiries_col.find_one({"_id": ObjectId(enquiry_id)})
    if not enq:
        return jsonify({"success": False, "message": "Enquiry not found."}), 404
    phone = _normalise_phone(enq.get("phone", ""))
    if not phone:
        return jsonify({"success": False, "message": "No phone number found on this enquiry."}), 400
    ok = send_whatsapp_msg(phone, message)
    if ok:
        _wa_append_message(enquiry_id=enquiry_id, direction="out", body=message, phone=phone)
    return jsonify({"success": ok, "message": "Sent." if ok else "WhatsApp delivery failed."})


@app.route("/api/admin/wa_thread/<enquiry_id>")
@login_required
@role_required("admin", "manager")
def wa_thread(enquiry_id):
    doc = wa_inbox_col.find_one({"enquiry_id": enquiry_id})
    if not doc:
        enq = enquiries_col.find_one({"_id": ObjectId(enquiry_id)})
        if enq:
            norm = _normalise_phone(enq.get("phone", ""))
            doc  = wa_inbox_col.find_one({"enquiry_id": f"unknown_{norm}"})
            if not doc and len(norm) == 12:
                doc = wa_inbox_col.find_one({"enquiry_id": f"unknown_{norm[2:]}"})
            if doc:
                wa_inbox_col.update_one({"_id": doc["_id"]}, {"$set": {"enquiry_id": enquiry_id}})
    if not doc:
        return jsonify({"messages": [], "push_name": "", "phone": ""})
    messages = []
    for m in doc.get("messages", []):
        messages.append({
            "direction":  m.get("direction", "in"),
            "body":       m.get("body", ""),
            "ts":         m["ts"].isoformat() if isinstance(m.get("ts"), datetime) else str(m.get("ts", "")),
            "message_id": m.get("message_id", ""),
        })
    return jsonify({"messages": messages, "push_name": doc.get("push_name", ""), "phone": doc.get("phone", "")})


@app.route("/api/webhook/wa_incoming", methods=["POST"])
def wa_incoming_webhook():
    data       = request.get_json(silent=True) or {}
    phone_raw  = (data.get("phoneNumber") or "").strip()
    body       = (data.get("body") or "").strip()
    push_name  = (data.get("pushName") or "").strip()
    message_id = (data.get("messageId") or "").strip()
    if not body:
        return jsonify({"status": "ignored", "reason": "empty body"}), 200
    norm_phone = _normalise_phone(phone_raw)
    phone_10   = norm_phone[2:] if len(norm_phone) == 12 and norm_phone.startswith("91") else norm_phone
    enq = None
    enquiry_id_str = (data.get("enquiry_id") or "").strip()
    if enquiry_id_str:
        try:
            enq = enquiries_col.find_one({"_id": ObjectId(enquiry_id_str)})
        except Exception:
            pass
    if not enq:
        enq = enquiries_col.find_one({"phone": {"$in": [norm_phone, phone_10, phone_raw]}, "tag": "form_enquiry"})
    if not enq:
        bucket_id = f"unknown_{norm_phone or phone_raw}"
        _wa_append_message(enquiry_id=bucket_id, direction="in", body=body,
                           phone=norm_phone, push_name=push_name, message_id=message_id)
        return jsonify({"status": "saved", "matched": False, "bucket": bucket_id}), 200
    enq_id = str(enq["_id"])
    _wa_append_message(enquiry_id=enq_id, direction="in", body=body,
                       phone=norm_phone, push_name=push_name, message_id=message_id)
    return jsonify({"status": "saved", "matched": True, "enquiry_id": enq_id}), 200


# ---------------------------------------------------------------------------
# BULK IMPORT / EXPORT
# ---------------------------------------------------------------------------
@app.route("/api/admin/bulk_enquiries_import", methods=["POST"])
@login_required
@role_required("admin", "manager")
def bulk_enquiries_import():
    data           = request.get_json(silent=True) or {}
    enquiries_list = data.get("enquiries", [])
    if not enquiries_list:
        return jsonify({"success": False, "message": "No enquiries provided."}), 400
    if not isinstance(enquiries_list, list):
        return jsonify({"success": False, "message": "enquiries must be a list."}), 400
    inserted = 0
    skipped  = 0
    errors   = []
    for idx, enq_data in enumerate(enquiries_list):
        try:
            name  = (enq_data.get("name") or "").strip()
            phone = (enq_data.get("phone") or "").strip()
            if not name or not phone:
                skipped += 1
                errors.append(f"Row {idx + 1}: Name and phone are required.")
                continue
            existing = enquiries_col.find_one({"phone": phone, "tag": "form_enquiry"})
            if existing:
                skipped += 1
                errors.append(f"Row {idx + 1}: Phone {phone} already exists.")
                continue
            plan_name      = (enq_data.get("plan") or "").strip()
            delivery_time  = (enq_data.get("delivery_time") or "").strip()
            start_date     = (enq_data.get("start_date") or "").strip()
            address        = (enq_data.get("address") or "").strip()
            payment_method = (enq_data.get("payment_method") or "upi").strip().lower()
            status         = (enq_data.get("status") or "pending").strip().lower()
            if status not in ["pending", "approved", "converted"]:
                status = "pending"
            if payment_method not in ["upi", "cod"]:
                payment_method = "upi"
            enquiries_col.insert_one({
                "name":           name,
                "phone":          phone,
                "address":        address,
                "delivery_time":  delivery_time,
                "start_date":     start_date,
                "plan":           plan_name,
                "payment_method": payment_method,
                "status":         status,
                "source":         "admin_bulk_import",
                "tag":            "form_enquiry",
                "created_at":     datetime.now(),
            })
            inserted += 1
        except Exception as e:
            skipped += 1
            errors.append(f"Row {idx + 1}: {str(e)}")
    return jsonify({
        "success":  True,
        "inserted": inserted,
        "skipped":  skipped,
        "total":    len(enquiries_list),
        "errors":   errors[:10],
        "message":  f"✅ Imported {inserted} enquiries. {skipped} skipped."
    })


@app.route("/api/admin/enquiries_export")
@login_required
@role_required("admin", "manager")
def enquiries_export():
    enquiries = list(enquiries_col.find({"tag": "form_enquiry"}).limit(1000))
    data = []
    for e in enquiries:
        data.append({
            "name":           e.get("name", ""),
            "phone":          e.get("phone", ""),
            "address":        e.get("address", ""),
            "plan":           e.get("plan", ""),
            "delivery_time":  e.get("delivery_time", ""),
            "start_date":     e.get("start_date", ""),
            "payment_method": e.get("payment_method", "upi"),
            "status":         e.get("status", "pending"),
        })
    return jsonify(data)


# ---------------------------------------------------------------------------
# EMPLOYEE ROUTES
# ---------------------------------------------------------------------------
@app.route("/employee")
@login_required
@role_required("employee")
def dashboard_employee():
    employee = users_col.find_one({"_id": ObjectId(session["user_id"])})
    if not employee or "team_id" not in employee:
        flash("You are not assigned to any team.", "danger")
        return redirect(url_for("home"))

    today_str  = datetime.now().strftime("%Y-%m-%d")
    emp_id_str = str(employee["_id"])
    today      = datetime.now().date()

    existing_deliveries = list(deliveries_col.find({
        "team_id": employee["team_id"],
        "date":    today_str
    }))
    delivery_map = {str(d["customer_id"]): d for d in existing_deliveries}

    customers = list(users_col.find({
        "role":    "customer",
        "team_id": employee["team_id"],
        "status":  "active",
    }))

    final_deliveries = []
    for cust in customers:
        cust_id_str = str(cust["_id"])
        plan = plans_col.find_one({"_id": ObjectId(cust["plan_id"])}) if cust.get("plan_id") else None

        # Use plan-aware scheduling (handles 22-day/26-day structural exclusions)
        if plan:
            if not _day_schedulable_for_plan(plan, today, cust.get("off_days", []), set(cust.get("holidays", []))):
                continue
        else:
            # Sample / no-plan customer: just check off_days + holidays
            day_name = today.strftime("%A")
            if day_name in cust.get("off_days", []):
                continue
            if today_str in cust.get("holidays", []):
                continue

        if cust_id_str in delivery_map:
            delivery = delivery_map[cust_id_str]
            if delivery.get("status") == "cancelled_holiday":
                continue
            delivery["customer"] = [cust]
            _enrich_delivery_plan(delivery, cust, today)
            final_deliveries.append(delivery)
        else:
            virtual_delivery = {
                "_id":             f"virtual_{cust_id_str}",
                "customer_id":     cust["_id"],
                "team_id":         employee["team_id"],
                "date":            today_str,
                "status":          "pending",
                "assigned_to":     None,
                "preferred_time":  cust.get("preferred_time", "Anytime"),
                "proof_photo_url": cust.get("latest_proof_photo_url") or None,
                "customer":        [cust]
            }
            _enrich_delivery_plan(virtual_delivery, cust, today)
            final_deliveries.append(virtual_delivery)

    def sort_key(d):
        if d["status"] == "accepted" and str(d.get("assigned_to", "")) == emp_id_str:
            return 0
        if d["status"] == "pending":
            return 1
        if d["status"] == "accepted":
            return 2
        return 3

    final_deliveries.sort(key=sort_key)
    return render_template(
        "dashboard_employee.html",
        deliveries=final_deliveries,
        photo_url="",
        cloudinary_cloud_name=app.config["CLOUDINARY_CLOUD_NAME"],
        cloudinary_upload_preset=app.config["CLOUDINARY_UPLOAD_PRESET"]
    )


@app.route("/employee/accept/<delivery_id>")
@login_required
@role_required("employee")
def accept_delivery(delivery_id):
    employee_id = ObjectId(session["user_id"])
    today_str   = datetime.now().strftime("%Y-%m-%d")
    if delivery_id.startswith("virtual_"):
        customer_id_str = delivery_id.replace("virtual_", "")
        customer        = users_col.find_one({"_id": ObjectId(customer_id_str)})
        exists          = deliveries_col.find_one({"customer_id": customer["_id"], "date": today_str})
        if not exists:
            deliveries_col.insert_one({
                "customer_id":    customer["_id"],
                "team_id":        customer["team_id"],
                "date":           today_str,
                "status":         "accepted",
                "assigned_to":    employee_id,
                "preferred_time": customer.get("preferred_time")
            })
    else:
        deliveries_col.update_one(
            {"_id": ObjectId(delivery_id), "status": "pending"},
            {"$set": {"status": "accepted", "assigned_to": employee_id}}
        )
    return redirect(url_for("dashboard_employee"))


@app.route("/employee/complete/<delivery_id>")
@login_required
@role_required("employee")
def complete_delivery(delivery_id):
    employee_id = ObjectId(session["user_id"])
    today_str   = datetime.now().strftime("%Y-%m-%d")
    if delivery_id.startswith("virtual_"):
        customer_id_str = delivery_id.replace("virtual_", "")
        customer        = users_col.find_one({"_id": ObjectId(customer_id_str)})
        exists          = deliveries_col.find_one({"customer_id": customer["_id"], "date": today_str})
        if not exists:
            deliveries_col.insert_one({
                "customer_id":    customer["_id"],
                "team_id":        customer["team_id"],
                "date":           today_str,
                "status":         "delivered",
                "assigned_to":    employee_id,
                "preferred_time": customer.get("preferred_time"),
                "delivered_at":   datetime.now()
            })
    else:
        deliveries_col.update_one(
            {"_id": ObjectId(delivery_id), "assigned_to": employee_id},
            {"$set": {"status": "delivered", "delivered_at": datetime.now()}}
        )
    return redirect(url_for("dashboard_employee"))

@app.route("/api/employee/accept_delivery/<delivery_id>", methods=["POST"])
@login_required
@role_required("employee")
def api_accept_delivery(delivery_id):
    """AJAX accept — no page redirect, returns JSON."""
    employee_id = ObjectId(session["user_id"])
    today_str   = datetime.now().strftime("%Y-%m-%d")
 
    if delivery_id.startswith("virtual_"):
        customer_id_str = delivery_id.replace("virtual_", "")
        try:
            customer = users_col.find_one({"_id": ObjectId(customer_id_str)})
        except Exception:
            return jsonify({"success": False, "message": "Invalid customer ID"}), 400
        if not customer:
            return jsonify({"success": False, "message": "Customer not found"}), 404
 
        existing = deliveries_col.find_one({"customer_id": customer["_id"], "date": today_str})
        if existing:
            if existing.get("status") == "accepted" and str(existing.get("assigned_to")) == str(employee_id):
                return jsonify({"success": True, "message": "Already accepted by you"})
            if existing.get("status") == "accepted":
                return jsonify({"success": False, "message": "Already accepted by someone else"}), 409
            deliveries_col.update_one(
                {"_id": existing["_id"]},
                {"$set": {"status": "accepted", "assigned_to": employee_id}}
            )
        else:
            deliveries_col.insert_one({
                "customer_id":    customer["_id"],
                "team_id":        customer.get("team_id"),
                "date":           today_str,
                "status":         "accepted",
                "assigned_to":    employee_id,
                "preferred_time": customer.get("preferred_time"),
            })
        return jsonify({"success": True, "message": f"Accepted delivery for {customer.get('name', '')}"})
 
    # Real delivery ID
    try:
        delivery = deliveries_col.find_one({"_id": ObjectId(delivery_id)})
    except Exception:
        return jsonify({"success": False, "message": "Invalid delivery ID"}), 400
    if not delivery:
        return jsonify({"success": False, "message": "Delivery not found"}), 404
    if delivery.get("status") == "accepted" and str(delivery.get("assigned_to")) == str(employee_id):
        return jsonify({"success": True, "message": "Already accepted by you"})
    if delivery.get("status") == "accepted":
        return jsonify({"success": False, "message": "Already accepted by someone else"}), 409
 
    result = deliveries_col.update_one(
        {"_id": ObjectId(delivery_id), "status": "pending"},
        {"$set": {"status": "accepted", "assigned_to": employee_id}}
    )
    if result.matched_count == 0:
        return jsonify({"success": False, "message": "Could not accept — status may have changed"}), 409
 
    return jsonify({"success": True, "message": "Delivery accepted"})
 
 
@app.route("/api/employee/complete_delivery/<delivery_id>", methods=["POST"])
@login_required
@role_required("employee")
def api_complete_delivery(delivery_id):
    """AJAX complete — no page redirect, returns JSON."""
    employee_id = ObjectId(session["user_id"])
    today_str   = datetime.now().strftime("%Y-%m-%d")
    now         = datetime.now()
 
    if delivery_id.startswith("virtual_"):
        customer_id_str = delivery_id.replace("virtual_", "")
        try:
            customer = users_col.find_one({"_id": ObjectId(customer_id_str)})
        except Exception:
            return jsonify({"success": False, "message": "Invalid customer ID"}), 400
        if not customer:
            return jsonify({"success": False, "message": "Customer not found"}), 404
 
        existing = deliveries_col.find_one({"customer_id": customer["_id"], "date": today_str})
        if existing:
            deliveries_col.update_one(
                {"_id": existing["_id"]},
                {"$set": {"status": "delivered", "assigned_to": employee_id, "delivered_at": now}}
            )
        else:
            deliveries_col.insert_one({
                "customer_id":    customer["_id"],
                "team_id":        customer.get("team_id"),
                "date":           today_str,
                "status":         "delivered",
                "assigned_to":    employee_id,
                "preferred_time": customer.get("preferred_time"),
                "delivered_at":   now,
            })
        return jsonify({"success": True, "message": f"Delivered to {customer.get('name', '')}"})
 
    # Real delivery ID
    try:
        delivery = deliveries_col.find_one({"_id": ObjectId(delivery_id)})
    except Exception:
        return jsonify({"success": False, "message": "Invalid delivery ID"}), 400
    if not delivery:
        return jsonify({"success": False, "message": "Delivery not found"}), 404
    if delivery.get("status") == "delivered":
        return jsonify({"success": True, "message": "Already marked delivered"})
 
    # Allow completing even if not assigned to this employee
    # (covers edge case of virtual→real transition)
    result = deliveries_col.update_one(
        {"_id": ObjectId(delivery_id)},
        {"$set": {"status": "delivered", "assigned_to": employee_id, "delivered_at": now}}
    )
    if result.matched_count == 0:
        return jsonify({"success": False, "message": "Delivery not found"}), 404
 
    return jsonify({"success": True, "message": "Delivery completed"})
 

# ---------------------------------------------------------------------------
# CUSTOMER ROUTES
# ---------------------------------------------------------------------------
@app.route("/customer")
@login_required
@role_required("customer")
def dashboard_customer():
    customer       = users_col.find_one({"_id": ObjectId(session["user_id"])})
    today_str      = datetime.now().strftime("%Y-%m-%d")
    today_delivery = deliveries_col.find_one({"customer_id": customer["_id"], "date": today_str})
    partner = None
    if today_delivery and today_delivery.get("assigned_to"):
        partner = users_col.find_one({"_id": today_delivery["assigned_to"]})
    now            = datetime.now()
    current_bill   = generate_monthly_bill(customer, now.month, now.year)
    estimated_bill = estimate_upcoming_bill(customer, now.month, now.year)
    is_sample      = bool(customer.get("sample"))
    sample_label   = customer.get("sample", "")
    return render_template(
        "dashboard_customer.html",
        delivery=today_delivery, partner=partner,
        current_bill=current_bill, estimated_bill=estimated_bill,
        is_sample=is_sample, sample_label=sample_label,
        datetime=datetime
    )


@app.route("/customer/mark_holiday", methods=["POST"])
@login_required
@role_required("customer")
def mark_holiday():
    date_str = request.form.get("date")
    users_col.update_one(
        {"_id": ObjectId(session["user_id"])},
        {"$addToSet": {"holidays": date_str}}
    )
    deliveries_col.update_one(
        {"customer_id": ObjectId(session["user_id"]), "date": date_str},
        {"$set": {"status": "cancelled_holiday"}}
    )
    flash(f"Marked {date_str} as holiday. No delivery.", "success")
    return redirect(url_for("dashboard_customer"))

#holiday tagging

@app.route("/api/customer/mark_holidays", methods=["POST"])
@login_required
@role_required("customer")
def api_customer_mark_holidays():
    data  = request.get_json(silent=True) or {}
    dates = data.get("dates", [])
    if isinstance(dates, str):
        dates = [dates]
    dates = [d.strip() for d in dates if d.strip()]
    if not dates:
        return jsonify({"success": False, "message": "No dates provided."}), 400
    valid, invalid = [], []
    for d in dates:
        try:
            datetime.strptime(d, "%Y-%m-%d")
            valid.append(d)
        except Exception:
            invalid.append(d)
    if not valid:
        return jsonify({"success": False, "message": f"Invalid dates: {invalid}"}), 400
    cust_id = ObjectId(session["user_id"])
    users_col.update_one({"_id": cust_id}, {"$addToSet": {"holidays": {"$each": valid}}})
    for d in valid:
        deliveries_col.update_one(
            {"customer_id": cust_id, "date": d},
            {"$set": {"status": "cancelled_holiday"}}
        )
    updated = users_col.find_one({"_id": cust_id}, {"holidays": 1})
    return jsonify({
        "success": True, "added": valid, "invalid": invalid,
        "holidays": sorted(updated.get("holidays", [])),
        "message": f"Marked {len(valid)} day(s) as holiday.",
    })


@app.route("/api/customer/holidays", methods=["GET"])
@login_required
@role_required("customer")
def api_customer_get_holidays():
    cust = users_col.find_one({"_id": ObjectId(session["user_id"])}, {"holidays": 1})
    holidays  = sorted((cust or {}).get("holidays", []))
    today_str = date.today().strftime("%Y-%m-%d")
    return jsonify({
        "success": True, "holidays": holidays,
        "upcoming": [d for d in holidays if d >= today_str],
        "past":     [d for d in holidays if d < today_str],
    })


@app.route("/api/customer/holidays/remove", methods=["POST"])
@login_required
@role_required("customer")
def api_customer_remove_holiday():
    data     = request.get_json(silent=True) or {}
    date_str = (data.get("date") or "").strip()
    if not date_str:
        return jsonify({"success": False, "message": "date required"}), 400
    cust_id = ObjectId(session["user_id"])
    users_col.update_one({"_id": cust_id}, {"$pull": {"holidays": date_str}})
    deliveries_col.update_many(
        {"customer_id": cust_id, "date": date_str, "status": "cancelled_holiday"},
        {"$set": {"status": "pending"}}
    )
    updated = users_col.find_one({"_id": cust_id}, {"holidays": 1})
    return jsonify({"success": True, "holidays": sorted(updated.get("holidays", []))})

# ---------------------------------------------------------------------------
# CRON – Daily deliveries
# ---------------------------------------------------------------------------
@app.route("/api/system/generate_daily", methods=["GET"])
def generate_daily_deliveries():
    today    = datetime.now().date()
    date_str = today.strftime("%Y-%m-%d")
    customers = users_col.find({"role": "customer", "status": "active"})
    count = 0
    for cust in customers:
        plan = plans_col.find_one({"_id": ObjectId(cust["plan_id"])}) if cust.get("plan_id") else None
        if plan:
            if not _day_schedulable_for_plan(plan, today, cust.get("off_days", []), set(cust.get("holidays", []))):
                continue
        else:
            day_name = today.strftime("%A")
            if day_name in cust.get("off_days", []):
                continue
            if date_str in cust.get("holidays", []):
                continue
        exists = deliveries_col.find_one({"customer_id": cust["_id"], "date": date_str})
        if not exists:
            deliveries_col.insert_one({
                "customer_id":    cust["_id"],
                "team_id":        cust.get("team_id"),
                "date":           date_str,
                "status":         "pending",
                "assigned_to":    None,
                "preferred_time": cust.get("preferred_time")
            })
            count += 1
    return jsonify({"status": "success", "generated_deliveries": count})


# ---------------------------------------------------------------------------
# CRON – Check invoices due today
# ---------------------------------------------------------------------------
@app.route("/api/system/check_invoice_due")
def check_invoice_due():
    token = request.args.get("token")
    if token != os.getenv("CRON_SECRET"):
        return jsonify({"error": "Unauthorized"}), 401
    today     = date.today()
    customers = users_col.find({"role": "customer", "status": "active"})
    generated = 0
    skipped   = 0
    failed    = 0
    for customer in customers:
        try:
            plan = plans_col.find_one({"_id": ObjectId(customer["plan_id"])}) if customer.get("plan_id") else None
            if not plan or int(plan.get("duration_days", 0)) <= 0:
                skipped += 1
                continue
            period_start, period_end = _calculate_invoice_period(customer, plan)
            if period_end != today:
                skipped += 1
                continue
            existing = invoices_col.find_one({
                "customer_id":  customer["_id"],
                "period_start": period_start.strftime("%Y-%m-%d"),
                "period_end":   period_end.strftime("%Y-%m-%d"),
            })
            if existing:
                skipped += 1
                continue
            schedulable, delivered = _billable_days_in_period(customer, period_start, period_end)
            price_total = float(plan.get("price_per_month", plan.get("price_per_day", 0)))
            tax_pct     = float(plan.get("tax_percent", 0))
            if schedulable > 0:
                subtotal = round((delivered / schedulable) * price_total, 2)
            else:
                subtotal = 0.0
            tax   = round(subtotal * tax_pct / 100, 2)
            gross = subtotal + tax
            disc_pct = float(customer.get("discount_percent", 0) or 0)
            disc_amt = float(customer.get("discount_amount",  0) or 0)
            discount, total = _apply_discount(gross, disc_pct, disc_amt)
            invoice = {
                "customer_id":      customer["_id"],
                "period_start":     period_start.strftime("%Y-%m-%d"),
                "period_end":       period_end.strftime("%Y-%m-%d"),
                "month":            period_end.month,
                "year":             period_end.year,
                "plan_name":        plan.get("name", ""),
                "duration_days":    int(plan.get("duration_days", 0)),
                "schedulable_days": schedulable,
                "billable_days":    delivered,
                "subtotal":         subtotal,
                "tax":              tax,
                "discount":         discount,
                "total":            total,
                "status":           "sent",
                "generated_at":     datetime.now(),
            }
            invoices_col.insert_one(invoice)
            _send_invoice_whatsapp(customer, invoice)
            # NEW: reset payment status for the new billing cycle
            users_col.update_one({"_id": customer["_id"]}, {"$set": {"payment_status": "pending", "payment_partial_amount": 0}})
            generated += 1
        except Exception as e:
            failed += 1
            print(f"INVOICE ERROR for {customer.get('name')}: {e}")
    return jsonify({
        "success":   True,
        "date":      today.strftime("%Y-%m-%d"),
        "generated": generated,
        "skipped":   skipped,
        "failed":    failed,
    })


# ---------------------------------------------------------------------------
# CRON – Legacy monthly bills
# ---------------------------------------------------------------------------
def get_previous_month_year():
    now = datetime.now()
    if now.month == 1:
        return 12, now.year - 1
    return now.month - 1, now.year


@app.route("/api/system/generate_monthly_bills")
def generate_monthly_bills():
    token = request.args.get("token")
    if token != os.getenv("CRON_SECRET"):
        return jsonify({"error": "Unauthorized"}), 401
    month, year = get_previous_month_year()
    customers   = users_col.find({"role": "customer"}, no_cursor_timeout=True).batch_size(100)
    generated   = 0
    failed      = 0
    for customer in customers:
        try:
            existing = invoices_col.find_one({
                "customer_id": customer["_id"],
                "month": month,
                "year":  year
            })
            if existing:
                continue
            estimate = estimate_upcoming_bill(customer, month, year)
            total    = generate_monthly_bill(customer, month, year)
            invoice  = {
                "customer_id":   customer["_id"],
                "month":         month,
                "year":          year,
                "billable_days": estimate["days"],
                "subtotal":      estimate["subtotal"],
                "tax":           estimate["tax"],
                "discount":      estimate.get("discount", 0),
                "total":         total,
                "status":        "sent",
                "generated_at":  datetime.now()
            }
            invoices_col.insert_one(invoice)
            _send_invoice_whatsapp(customer, invoice)
            # NEW: reset payment status for the new billing cycle
            users_col.update_one({"_id": customer["_id"]}, {"$set": {"payment_status": "pending", "payment_partial_amount": 0}})
            generated += 1
        except Exception as e:
            failed += 1
            print("INVOICE ERROR:", str(e))
    return jsonify({"success": True, "generated": generated, "failed": failed})


@app.route("/test-invoice", methods=["GET", "POST"])
def test_invoice():
    if request.method == "POST":
        customer = {"name": request.form.get("name"), "email": request.form.get("email")}
        invoice  = {
            "month": 5, "year": 2026,
            "period_start": "2026-05-01", "period_end": "2026-05-26",
            "plan_name": "Test Plan",
            "billable_days": 24, "subtotal": 2400, "tax": 120, "discount": 0, "total": 2520
        }
        _send_invoice_whatsapp(customer, invoice)
        flash("Mock invoice sent via WhatsApp!", "success")
        return redirect(url_for("test_invoice"))
    return render_template("test_invoice.html")


# ---------------------------------------------------------------------------
# ADMIN – Boxes needed today/tomorrow
# ---------------------------------------------------------------------------
@app.route("/api/admin/boxes_needed")
@login_required
@role_required("admin", "manager")
def api_boxes_needed():
    now = datetime.now()
    tf  = _manager_team_filter()

    if now.hour >= 13:
        target_date = (now + timedelta(days=1)).date()
        is_next_day = True
    else:
        target_date = now.date()
        is_next_day = False

    target_str = target_date.strftime("%Y-%m-%d")

    plans  = list(plans_col.find())
    teams  = list(teams_col.find())

    base_cust_q = {"role": "customer", "status": "active", "plan_id": {"$exists": True, "$ne": None}}
    if tf: base_cust_q.update(tf)
    active_customers = list(users_col.find(base_cust_q))

    sample_q = {"role": "customer", "status": "active", "sample": {"$exists": True, "$nin": [None, ""]}}
    if tf: sample_q.update(tf)
    sample_customers = list(users_col.find(sample_q))

    # NEW: union of all active customers — used for New-customer & Holiday tagging
    all_active_q = {"role": "customer", "status": "active"}
    if tf: all_active_q.update(tf)
    all_active_customers = list(users_col.find(all_active_q))

    team_map = {str(t["_id"]): t for t in teams}
    plan_map = {str(p["_id"]): p for p in plans}
    result   = []

    for plan in plans:
        plan_id   = str(plan["_id"])
        plan_name = plan.get("name", "")
        alt_items = plan.get("alternate_items", [])
        is_alt    = bool(alt_items and len(alt_items) >= 2)
        alt_label = _get_alternate_item_for_date(plan, target_date) if is_alt else None

        team_breakdown = {}
        active_count   = 0
        global_alt_counts = {}

        for c in active_customers:
            if str(c.get("plan_id", "")) != plan_id:
                continue
            if not _day_schedulable_for_plan(plan, target_date, c.get("off_days", []), set(c.get("holidays", []))):
                continue

            active_count += 1
            cust_alt_options = c.get("alt_options", [])
            cust_item_for_count = _get_customer_item_for_date(c, target_date) if cust_alt_options else None

            if cust_item_for_count:
                global_alt_counts[cust_item_for_count] = global_alt_counts.get(cust_item_for_count, 0) + 1

            tid = str(c.get("team_id", "unassigned"))
            if tid not in team_breakdown:
                team_obj = team_map.get(tid, {})
                team_breakdown[tid] = {
                    "team_id": tid, "team_name": team_obj.get("name", "Unassigned"),
                    "team_area": team_obj.get("area", ""), "count": 0, "alt_counts": {},
                }
            team_breakdown[tid]["count"] += 1
            if cust_item_for_count:
                tc = team_breakdown[tid]["alt_counts"]
                tc[cust_item_for_count] = tc.get(cust_item_for_count, 0) + 1

        matched_sample_count = 0
        for c in sample_customers:
            if str(c.get("plan_id", "")) == plan_id:
                day_name = target_date.strftime("%A")
                if day_name not in c.get("off_days", []) and target_str not in c.get("holidays", []):
                    matched_sample_count += 1

        pending_enq_count = 0
        if not tf:
            pending_enq_count = enquiries_col.count_documents({
                "tag": "form_enquiry", "status": "pending",
                "plan": {"$regex": re.escape(plan_name[:20]), "$options": "i"}
            }) if plan_name else 0

        # NEW: customers on this plan who are on holiday for target_date
        holiday_count = 0
        for c in active_customers:
            if str(c.get("plan_id", "")) != plan_id:
                continue
            day_name = target_date.strftime("%A")
            if (target_str in set(c.get("holidays", []))) or (day_name in c.get("off_days", [])):
                holiday_count += 1

        if active_count == 0 and matched_sample_count == 0 and pending_enq_count == 0 and holiday_count == 0:
            continue

        result.append({
            "plan_id": plan_id, "plan_name": plan_name,
            "plan_price": plan.get("price_per_month") or plan.get("price_per_day") or 0,
            "duration_days": plan.get("duration_days", 0),
            "active_customers": active_count, "sample_boxes": matched_sample_count,
            "pending_enquiries": pending_enq_count, "total_boxes": active_count + matched_sample_count,
            "holiday_customers": holiday_count,
            "teams": sorted(team_breakdown.values(), key=lambda x: -x["count"]),
            "is_sample_plan": False, "is_alternate_plan": is_alt,
            "alternate_items": alt_items, "today_item": alt_label,
            "global_alt_counts": global_alt_counts,
            "is_next_day": is_next_day, "target_date": target_str,
        })

    unmatched_samples = {}
    for c in sample_customers:
        if c.get("plan_id"): continue
        day_name = target_date.strftime("%A")
        if day_name in c.get("off_days", []): continue
        if target_str in c.get("holidays", []): continue
        label = c.get("sample", "Sample")
        unmatched_samples[label] = unmatched_samples.get(label, 0) + 1

    for label, count in unmatched_samples.items():
        result.append({
            "plan_id": None, "plan_name": label, "plan_price": None, "duration_days": None,
            "active_customers": 0, "sample_boxes": count, "pending_enquiries": 0,
            "total_boxes": count, "holiday_customers": 0, "teams": [], "is_sample_plan": True, "is_alternate_plan": False,
            "alternate_items": [], "today_item": None, "global_alt_counts": {},
            "is_next_day": is_next_day, "target_date": target_str,
        })

    # NEW: newly tagged customers
    new_customers = []
    for c in all_active_customers:
        if not c.get("is_new"):
            continue
        plan = plan_map.get(str(c.get("plan_id", "")), {})
        new_customers.append({
            "id": str(c["_id"]), "name": c.get("name", ""), "phone": c.get("phone", ""),
            "address": c.get("address", ""),
            "plan_name": plan.get("name") or c.get("sample") or "—",
            "team_id": str(c.get("team_id", "")),
        })

    # NEW: customers on holiday for target date
    holiday_customers = []
    for c in all_active_customers:
        holidays = set(c.get("holidays", []))
        off_days = c.get("off_days", [])
        day_name = target_date.strftime("%A")
        if target_str in holidays or day_name in off_days:
            plan = plan_map.get(str(c.get("plan_id", "")), {})
            holiday_customers.append({
                "id": str(c["_id"]), "name": c.get("name", ""), "phone": c.get("phone", ""),
                "plan_name": plan.get("name") or c.get("sample") or "—",
                "reason": "holiday" if target_str in holidays else "off_day",
            })

    return jsonify({
        "plans": result,
        "new_customers": new_customers,
        "holiday_customers": holiday_customers,
        "is_next_day": is_next_day,
        "target_date": target_str,
    })

# ---------------------------------------------------------------------------
# ADMIN – Customer Holiday Management
# ---------------------------------------------------------------------------
@app.route("/api/admin/customer/<customer_id>/holidays", methods=["GET"])
@login_required
@role_required("admin", "manager")
def api_get_customer_holidays(customer_id):
    try:
        customer = users_col.find_one({"_id": ObjectId(customer_id)}, {"holidays": 1, "name": 1})
    except Exception:
        return jsonify({"success": False, "message": "Invalid ID"}), 400
    if not customer:
        return jsonify({"success": False, "message": "Not found"}), 404
    return jsonify({
        "success":  True,
        "holidays": sorted(customer.get("holidays", [])),
        "name":     customer.get("name", ""),
    })


@app.route("/api/admin/customer/<customer_id>/holidays/add", methods=["POST"])
@login_required
@role_required("admin", "manager")
def api_add_customer_holiday(customer_id):
    data  = request.get_json(silent=True) or {}
    dates = data.get("dates", [])
    if isinstance(dates, str):
        dates = [dates]
    dates = [d.strip() for d in dates if d.strip()]
    if not dates:
        return jsonify({"success": False, "message": "No dates provided"}), 400
    # Validate date format
    valid, invalid = [], []
    for d in dates:
        try:
            datetime.strptime(d, "%Y-%m-%d")
            valid.append(d)
        except Exception:
            invalid.append(d)
    if not valid:
        return jsonify({"success": False, "message": f"Invalid dates: {invalid}"}), 400
    users_col.update_one(
        {"_id": ObjectId(customer_id)},
        {"$addToSet": {"holidays": {"$each": valid}}}
    )
    # Cancel any pending deliveries on those days
    for d in valid:
        deliveries_col.update_many(
            {"customer_id": ObjectId(customer_id), "date": d, "status": {"$in": ["pending", "accepted"]}},
            {"$set": {"status": "cancelled_holiday"}}
        )
    updated = users_col.find_one({"_id": ObjectId(customer_id)}, {"holidays": 1})
    return jsonify({
        "success":  True,
        "added":    valid,
        "invalid":  invalid,
        "holidays": sorted(updated.get("holidays", [])),
        "message":  f"Added {len(valid)} holiday(s).",
    })


@app.route("/api/admin/customer/<customer_id>/holidays/remove", methods=["POST"])
@login_required
@role_required("admin", "manager")
def api_remove_customer_holiday(customer_id):
    data     = request.get_json(silent=True) or {}
    date_str = data.get("date", "").strip()
    if not date_str:
        return jsonify({"success": False, "message": "date required"}), 400
    users_col.update_one(
        {"_id": ObjectId(customer_id)},
        {"$pull": {"holidays": date_str}}
    )
    # Restore delivery to pending if it was cancelled for holiday
    deliveries_col.update_many(
        {"customer_id": ObjectId(customer_id), "date": date_str, "status": "cancelled_holiday"},
        {"$set": {"status": "pending"}}
    )
    updated = users_col.find_one({"_id": ObjectId(customer_id)}, {"holidays": 1})
    return jsonify({
        "success":  True,
        "removed":  date_str,
        "holidays": sorted(updated.get("holidays", [])),
        "message":  f"Removed holiday {date_str}.",
    })

# ---------------------------------------------------------------------------
# ADMIN – Customer plan progress (FIXED for 22/26-day plans)
# ---------------------------------------------------------------------------
@app.route("/api/admin/customer_plan_status/<customer_id>")
@login_required
@role_required("admin", "manager")
def customer_plan_status(customer_id):
    try:
        customer = users_col.find_one({"_id": ObjectId(customer_id)})
    except Exception:
        return jsonify({"success": False, "message": "Invalid customer ID"}), 400

    if not customer:
        return jsonify({"success": False, "message": "Customer not found"}), 404

    plan = plans_col.find_one({"_id": ObjectId(customer["plan_id"])}) if customer.get("plan_id") else None
    if not plan:
        return jsonify({"success": False, "has_plan": False})

    duration_days = int(plan.get("duration_days", 0))
    start_date = _parse_start_date(customer)

    if not start_date or duration_days <= 0:
        return jsonify({
            "success": False,
            "has_plan": False,
            "message": "No valid start date or duration"
        })

    period_start = start_date
    period_end = start_date + timedelta(days=duration_days - 1)
    today = date.today()

    delivered_count = deliveries_col.count_documents({
        "customer_id": customer["_id"],
        "status": "delivered"
    })

    # If admin has set a manual override on the customer document, add it
    extra = customer.get("total_delivered_boxes")
    if extra is not None:
        try:
            delivered_count += int(extra)
        except (TypeError, ValueError):
            pass

    remaining_days = max(0, duration_days - delivered_count)

    completion_pct = round(
        (delivered_count / duration_days) * 100
    ) if duration_days else 0

    return jsonify({
        "success": True,
        "has_plan": True,
        "plan_name": plan.get("name", ""),
        "period_start": period_start.strftime("%Y-%m-%d"),
        "period_end": period_end.strftime("%Y-%m-%d"),
        "duration_days": duration_days,
        "delivered": delivered_count,
        "remaining_days": remaining_days,
        "completion_pct": completion_pct,
        "today": today.strftime("%Y-%m-%d"),
        "plan_price": float(
            plan.get("price_per_month")
            or plan.get("price_per_day")
            or 0
        ),
        "is_alternate_plan": bool(
            plan.get("alternate_items")
            and len(plan.get("alternate_items", [])) >= 2
        ),
        "alternate_items": plan.get("alternate_items", []),
        "today_item": _get_alternate_item_for_date(plan, today),
    })



# ---------------------------------------------------------------------------
# ADMIN – Sample customers detail for Boxes view
# ---------------------------------------------------------------------------
@app.route("/api/admin/sample_customers")
@login_required
@role_required("admin", "manager")
def api_sample_customers():
    label     = request.args.get("label", "").strip()
    plan_id   = request.args.get("plan_id", "").strip()
    today     = datetime.now().date()
    today_str = today.strftime("%Y-%m-%d")
    query = {
        "role":   "customer",
        "status": "active",
        "sample": {"$exists": True, "$nin": [None, ""]},
    }
    if plan_id:
        try:
            query["plan_id"] = ObjectId(plan_id)
        except Exception:
            pass
    elif label:
        query["sample"] = label

    customers = list(users_col.find(query))
    teams     = list(teams_col.find())
    team_map  = {str(t["_id"]): t for t in teams}
    result_customers = []
    for c in customers:
        day_name = today.strftime("%A")
        if day_name in c.get("off_days", []):
            continue
        if today_str in c.get("holidays", []):
            continue
        team = team_map.get(str(c.get("team_id", "")), {})
        result_customers.append({
            "id":             str(c["_id"]),
            "name":           c.get("name", ""),
            "phone":          c.get("phone", ""),
            "address":        c.get("address", ""),
            "preferred_time": c.get("preferred_time", ""),
            "sample_label":   c.get("sample", label or "Sample"),
            "team_id":        str(c.get("team_id", "")),
            "team_name":      team.get("name", "—"),
        })
    result_teams = [{"id": str(t["_id"]), "name": t.get("name", ""), "area": t.get("area", "")} for t in teams]
    return jsonify({"customers": result_customers, "teams": result_teams})


@app.route("/api/admin/assign_team", methods=["POST"])
@login_required
@role_required("admin", "manager")
def assign_team_to_customer():
    data        = request.get_json(silent=True) or {}
    customer_id = data.get("customer_id")
    team_id     = data.get("team_id")
    if not customer_id or not team_id:
        return jsonify({"success": False, "message": "Missing customer_id or team_id"}), 400
    try:
        users_col.update_one(
            {"_id": ObjectId(customer_id)},
            {"$set": {"team_id": ObjectId(team_id)}}
        )
        team = teams_col.find_one({"_id": ObjectId(team_id)})
        return jsonify({"success": True, "team_name": team.get("name","") if team else ""})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/admin/managers")
@login_required
@role_required("admin")
def api_managers():
    ff       = _franchise_filter()
    query    = {"role": "manager", **ff}
    managers = list(users_col.find(query))
    teams    = list(teams_col.find(ff))
    team_map = {str(t["_id"]): t for t in teams}
    result = []
    for m in managers:
        tids       = m.get("team_ids") or ([m["team_id"]] if m.get("team_id") else [])
        tids       = [str(t) for t in tids]
        team_names = [team_map[t]["name"] for t in tids if t in team_map]
        result.append({
            "id":         str(m["_id"]),
            "name":       m.get("name", ""),
            "email":      m.get("email", ""),
            "phone":      m.get("phone", "—"),
            "team_ids":   tids,
            "team_names": team_names,
        })
    return jsonify(result)


@app.route("/admin/managers/assign-team", methods=["POST"])
@login_required
@role_required("admin")
def assign_team_to_manager():
    data       = request.get_json(silent=True) or {}
    manager_id = data.get("manager_id")
    team_ids   = data.get("team_ids", [])
    if not manager_id:
        return jsonify({"success": False, "message": "manager_id required"}), 400
    oids = [ObjectId(t) for t in team_ids if t]
    users_col.update_one(
        {"_id": ObjectId(manager_id), "role": "manager"},
        {"$set": {"team_ids": oids, "team_id": oids[0] if oids else None}}
    )
    names = [t.get("name", "") for t in teams_col.find({"_id": {"$in": oids}})]
    return jsonify({"success": True, "team_names": names})

@app.route("/api/admin/assign_manager", methods=["POST"])
@login_required
@role_required("admin", "manager")
def assign_manager_to_user():
    data      = request.get_json(silent=True) or {}
    target_id = data.get("target_id")
    manager_id = data.get("manager_id")  # "" clears assignment
    if not target_id:
        return jsonify({"success": False, "message": "target_id required"}), 400
    try:
        update = {"assigned_manager_id": ObjectId(manager_id) if manager_id else None}
        users_col.update_one({"_id": ObjectId(target_id)}, {"$set": update})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 400
    mgr = users_col.find_one({"_id": ObjectId(manager_id)}) if manager_id else None
    return jsonify({"success": True, "manager_name": mgr.get("name", "") if mgr else ""})

# ---------------------------------------------------------------------------
# N8N – Add enquiry from WhatsApp
# ---------------------------------------------------------------------------
@app.route("/api/enq/add_enquiry", methods=["POST"])
def n8n_add_enquiry():
    token = request.headers.get("X-Api-Key") or request.args.get("token")
    if token != os.getenv("Token_SECRET"):
        return jsonify({"error": "Unauthorized"}), 401
    data      = request.get_json(silent=True) or {}
    push_name = (data.get("pushName") or "").strip()
    body      = (data.get("body") or "").strip()
    phone_raw = (data.get("phoneNumber") or data.get("from") or "").strip()
    if not phone_raw:
        return jsonify({"success": False, "message": "Missing 'phoneNumber'."}), 400
    norm_phone = _normalise_phone(phone_raw)
    phone_10   = norm_phone[2:] if len(norm_phone) == 12 and norm_phone.startswith("91") else norm_phone
    existing = enquiries_col.find_one({
        "phone":  {"$in": [norm_phone, phone_10]},
        "tag":    "form_enquiry",
        "status": "pending"
    })
    if existing:
        return jsonify({"success": False, "message": "Pending enquiry already exists.", "enquiry_id": str(existing["_id"])}), 409
    enquiry_doc = {
        "name":       push_name,
        "phone":      norm_phone,
        "source":     "whatsapp",
        "tag":        "form_enquiry",
        "status":     "pending",
        "created_at": datetime.now(),
    }
    result = enquiries_col.insert_one(enquiry_doc)
    enq_id = str(result.inserted_id)
    if body:
        _wa_append_message(enquiry_id=enq_id, direction="in", body=body,
                           phone=norm_phone, push_name=push_name)
    return jsonify({
        "success":    True,
        "enquiry_id": enq_id,
        "name":       push_name or norm_phone,
        "phone":      norm_phone,
        "message":    "Enquiry created."
    }), 201


# ---------------------------------------------------------------------------
# EMPLOYEE – GPS location
# ---------------------------------------------------------------------------
@app.route("/api/employee/update_location", methods=["POST"])
@login_required
@role_required("employee")
def update_customer_location():
    data        = request.get_json(silent=True) or {}
    customer_id = data.get("customer_id", "").strip()
    lat         = data.get("lat")
    lng         = data.get("lng")
    if not customer_id:
        return jsonify({"success": False, "message": "customer_id is required."}), 400
    if lat is None or lng is None:
        return jsonify({"success": False, "message": "lat and lng are required."}), 400
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "lat and lng must be numbers."}), 400
    employee = users_col.find_one({"_id": ObjectId(session["user_id"])})
    customer = users_col.find_one({"_id": ObjectId(customer_id), "role": "customer"})
    if not customer:
        return jsonify({"success": False, "message": "Customer not found."}), 404
    if employee and employee.get("team_id") and str(customer.get("team_id")) != str(employee.get("team_id")):
        return jsonify({"success": False, "message": "Unauthorized: not your team's customer."}), 403
    users_col.update_one(
        {"_id": ObjectId(customer_id)},
        {"$set": {
            "delivery_lat":         lat,
            "delivery_lng":         lng,
            "delivery_loc_updated": datetime.now(),
            "delivery_loc_by":      session.get("name", session["user_id"]),
        }}
    )
    today_str = datetime.now().strftime("%Y-%m-%d")
    deliveries_col.update_one(
        {"customer_id": ObjectId(customer_id), "date": today_str},
        {"$set": {
            "delivery_lat":   lat,
            "delivery_lng":   lng,
            "loc_updated_at": datetime.now(),
            "loc_updated_by": session.get("name", session["user_id"]),
        }}
    )
    return jsonify({
        "success": True,
        "message": f"Location saved for {customer.get('name', '')}.",
        "lat": lat,
        "lng": lng,
    })


@app.route("/api/employee/customer_location/<customer_id>")
@login_required
@role_required("employee", "admin", "manager")
def get_customer_location(customer_id):
    customer = users_col.find_one(
        {"_id": ObjectId(customer_id)},
        {"delivery_lat": 1, "delivery_lng": 1, "delivery_loc_updated": 1, "delivery_loc_by": 1, "name": 1}
    )
    if not customer:
        return jsonify({"success": False, "message": "Not found."}), 404
    lat     = customer.get("delivery_lat")
    lng     = customer.get("delivery_lng")
    updated = customer.get("delivery_loc_updated")
    return jsonify({
        "success":      True,
        "has_location": lat is not None and lng is not None,
        "lat":          lat,
        "lng":          lng,
        "updated_at":   updated.isoformat() if isinstance(updated, datetime) else None,
        "updated_by":   customer.get("delivery_loc_by", ""),
        "name":         customer.get("name", ""),
    })



@app.route("/api/employee/my_boxes")
@login_required
@role_required("employee")
def employee_my_boxes():
    employee = users_col.find_one({"_id": ObjectId(session["user_id"])})
    if not employee or not employee.get("team_id"):
        return jsonify({"success": False, "message": "Not assigned to a team"}), 400
    now = datetime.now()
    if now.hour >= 13:
        target_date = (now + timedelta(days=1)).date()
        is_next_day = True
    else:
        target_date = now.date()
        is_next_day = False
    target_str = target_date.strftime("%Y-%m-%d")
    team_id    = employee["team_id"]
    team       = teams_col.find_one({"_id": team_id})
    customers  = list(users_col.find({
        "role":    "customer",
        "status":  "active",
        "team_id": team_id,
    }))
    plans    = list(plans_col.find())
    plan_map = {str(p["_id"]): p for p in plans}
    plan_counts   = {}
    sample_counts = {}
    total         = 0
    for c in customers:
        if c.get("plan_id") and not c.get("sample"):
            pid  = str(c["plan_id"])
            plan = plan_map.get(pid)
            if not plan:
                continue
            if not _day_schedulable_for_plan(plan, target_date, c.get("off_days", []), set(c.get("holidays", []))):
                continue
            alt_items = plan.get("alternate_items", [])
            is_alt    = bool(alt_items and len(alt_items) >= 2)
            alt_label = _get_alternate_item_for_date(plan, target_date) if is_alt else None
            if pid not in plan_counts:
                plan_counts[pid] = {
                    "plan_id":   pid,
                    "plan_name": plan.get("name", ""),
                    "count":     0,
                    "is_alt":    is_alt,
                    "alt_item":  alt_label,
                    "price":     float(plan.get("price_per_month") or plan.get("price_per_day") or 0),
                }
            plan_counts[pid]["count"] += 1
            total += 1
        elif c.get("sample"):
            day_name = target_date.strftime("%A")
            if day_name in c.get("off_days", []):
                continue
            if target_str in c.get("holidays", []):
                continue
            label = c.get("sample", "Sample")
            sample_counts[label] = sample_counts.get(label, 0) + 1
            total += 1
    return jsonify({
        "success":     True,
        "is_next_day": is_next_day,
        "target_date": target_str,
        "team_name":   team.get("name", "") if team else "",
        "team_area":   team.get("area", "") if team else "",
        "total":       total,
        "plans":       sorted(plan_counts.values(), key=lambda x: -x["count"]),
        "samples":     [{"label": k, "count": v} for k, v in sample_counts.items()],
    })


@app.route("/api/employee/save_proof_photo", methods=["POST"])
@login_required
@role_required("employee")
def save_proof_photo():
    data        = request.get_json(silent=True) or {}
    delivery_id = data.get("delivery_id", "").strip()
    photo_url   = data.get("photo_url", "").strip()
    if not delivery_id or not photo_url:
        return jsonify({"success": False, "message": "delivery_id and photo_url are required."}), 400
    if "cloudinary.com" not in photo_url and not photo_url.startswith("https://"):
        return jsonify({"success": False, "message": "Invalid photo URL."}), 400
    uploader_name = session.get("name", session["user_id"])
    now           = datetime.now()
    if delivery_id.startswith("virtual_"):
        customer_id_str = delivery_id.replace("virtual_", "")
        today_str       = now.strftime("%Y-%m-%d")
        employee_id     = ObjectId(session["user_id"])
        try:
            customer = users_col.find_one({"_id": ObjectId(customer_id_str)})
        except Exception:
            return jsonify({"success": False, "message": "Customer not found."}), 404
        if not customer:
            return jsonify({"success": False, "message": "Customer not found."}), 404
        existing = deliveries_col.find_one({"customer_id": customer["_id"], "date": today_str})
        if existing:
            real_delivery_id = str(existing["_id"])
            deliveries_col.update_one(
                {"_id": existing["_id"]},
                {"$set": {
                    "proof_photo_url":   photo_url,
                    "photo_uploaded_at": now,
                    "photo_uploaded_by": uploader_name,
                }}
            )
        else:
            result = deliveries_col.insert_one({
                "customer_id":       customer["_id"],
                "team_id":           customer.get("team_id"),
                "date":              today_str,
                "status":            "pending",
                "assigned_to":       employee_id,
                "preferred_time":    customer.get("preferred_time"),
                "proof_photo_url":   photo_url,
                "photo_uploaded_at": now,
                "photo_uploaded_by": uploader_name,
            })
            real_delivery_id = str(result.inserted_id)
        users_col.update_one(
            {"_id": customer["_id"]},
            {
                "$set": {
                    "latest_proof_photo_url": photo_url,
                    "latest_photo_at":        now,
                    "latest_photo_by":        uploader_name,
                },
                "$push": {
                    "photo_log": {
                        "url":         photo_url,
                        "uploaded_at": now,
                        "uploaded_by": uploader_name,
                        "delivery_id": real_delivery_id,
                        "date":        today_str,
                    }
                },
            }
        )
        return jsonify({"success": True, "message": "Photo saved.", "photo_url": photo_url})

    try:
        delivery = deliveries_col.find_one({"_id": ObjectId(delivery_id)})
    except Exception:
        return jsonify({"success": False, "message": "Invalid delivery_id."}), 400
    if not delivery:
        return jsonify({"success": False, "message": "Delivery not found."}), 404
    employee = users_col.find_one({"_id": ObjectId(session["user_id"])})
    if (employee and employee.get("team_id")
            and str(delivery.get("team_id")) != str(employee.get("team_id"))):
        return jsonify({"success": False, "message": "Unauthorized: not your team's delivery."}), 403
    deliveries_col.update_one(
        {"_id": ObjectId(delivery_id)},
        {"$set": {
            "proof_photo_url":   photo_url,
            "photo_uploaded_at": now,
            "photo_uploaded_by": uploader_name,
        }}
    )
    today_str = now.strftime("%Y-%m-%d")
    users_col.update_one(
        {"_id": delivery["customer_id"]},
        {
            "$set": {
                "latest_proof_photo_url": photo_url,
                "latest_photo_at":        now,
                "latest_photo_by":        uploader_name,
            },
            "$push": {
                "photo_log": {
                    "url":         photo_url,
                    "uploaded_at": now,
                    "uploaded_by": uploader_name,
                    "delivery_id": delivery_id,
                    "date":        today_str,
                }
            },
        }
    )
    return jsonify({"success": True, "message": "Photo saved.", "photo_url": photo_url})

#my plan status

@app.route("/api/customer/my_plan_status")
@login_required
@role_required("customer")
def customer_my_plan_status():
    customer = users_col.find_one({"_id": ObjectId(session["user_id"])})
    if not customer:
        return jsonify({"success": False, "message": "Customer not found"}), 404

    plan = plans_col.find_one({"_id": ObjectId(customer["plan_id"])}) if customer.get("plan_id") else None
    if not plan:
        return jsonify({"success": False, "has_plan": False})

    duration_days = int(plan.get("duration_days", 0))
    start_date = _parse_start_date(customer)
    if not start_date or duration_days <= 0:
        return jsonify({"success": False, "has_plan": False, "message": "No valid start date or duration"})

    period_start = start_date
    period_end = start_date + timedelta(days=duration_days - 1)
    today = date.today()

    # Get all deliveries in period with their statuses
    deliveries_in_period = list(deliveries_col.find({
        "customer_id": customer["_id"],
        "date": {
            "$gte": period_start.strftime("%Y-%m-%d"),
            "$lte": period_end.strftime("%Y-%m-%d")
        }
    }))
    delivery_status_map = {d["date"]: d["status"] for d in deliveries_in_period}

    off_days = customer.get("off_days", [])
    holidays = set(customer.get("holidays", []))

    boxes = []
    current = period_start
    while current <= period_end:
        date_str = current.strftime("%Y-%m-%d")
        day_name = current.strftime("%A")
        d_status = delivery_status_map.get(date_str)

        if d_status == "delivered":
            box_status = "delivered"
        elif d_status == "cancelled_holiday" or date_str in holidays or day_name in off_days:
            box_status = "holiday"
        elif current < today:
            box_status = "missed"
        elif current == today:
            box_status = "today"
        else:
            box_status = "upcoming"

        boxes.append({
            "date":     date_str,
            "day":      day_name[:3],
            "status":   box_status,
            "is_holiday": date_str in holidays,  # explicit flag for UI
        })
        current += timedelta(days=1)

    delivered_count = sum(1 for b in boxes if b["status"] == "delivered")
    remaining_days = max(0, duration_days - delivered_count)
    completion_pct = round((delivered_count / duration_days) * 100) if duration_days else 0

    return jsonify({
        "success": True,
        "has_plan": True,
        "plan_name": plan.get("name", ""),
        "period_start": period_start.strftime("%Y-%m-%d"),
        "period_end": period_end.strftime("%Y-%m-%d"),
        "duration_days": duration_days,
        "delivered": delivered_count,
        "remaining_days": remaining_days,
        "completion_pct": completion_pct,
        "today": today.strftime("%Y-%m-%d"),
        "plan_price": float(plan.get("price_per_month") or plan.get("price_per_day") or 0),
        "is_alternate_plan": bool(plan.get("alternate_items") and len(plan.get("alternate_items", [])) >= 2),
        "alternate_items": plan.get("alternate_items", []),
        "today_item": _get_alternate_item_for_date(plan, today),
        "holidays": sorted(list(holidays)),
        "boxes": boxes,
    })


@app.route("/api/customer/plan_history")
@login_required
@role_required("customer")
def api_customer_plan_history():
    """
    Full manual-billing history for the logged-in customer — each closed
    period frozen exactly as it was billed. The LIVE current-cycle timeline
    stays in /api/customer/my_plan_status (always the new start_date, filled
    in as delivery partners mark today's deliveries).
    """
    cust_id = ObjectId(session["user_id"])
    bills = list(manual_bills_col.find({"customer_id": cust_id}).sort("created_at", -1).limit(24))
    return jsonify({"success": True, "history": [_serialize_manual_bill(b) for b in bills]})


#manuall plan view

@app.route("/api/customer/manual_bills")
@login_required
@role_required("customer")
def api_customer_manual_bills():
    bills = list(
        manual_bills_col.find({"customer_id": ObjectId(session["user_id"])})
        .sort("created_at", -1).limit(12)
    )
    result = []
    for b in bills:
        result.append({
            "id":               str(b["_id"]),
            "bill_month_label": b.get("bill_month_label", ""),
            "period_start":     b.get("period_start", ""),
            "period_end":       b.get("period_end", ""),
            "working_days":     b.get("working_days", 0),
            "leave_days":       b.get("leave_days", 0),
            "rate_per_day":     b.get("rate_per_day", 0),
            "subtotal":         b.get("subtotal", 0),
            "discount":         b.get("discount", 0),
            "total":            b.get("total", 0),
            "sent":             b.get("sent", False),
            "created_at":       b["created_at"].strftime("%Y-%m-%d") if isinstance(b.get("created_at"), datetime) else "",
        })
    return jsonify({"success": True, "bills": result})

#Bulk sending api


@app.route("/admin/bulk-whatsapp")
@login_required
@role_required("admin", "manager")
def bulk_whatsapp_page():
    tf        = _manager_team_filter()
    cust_q    = {"role": "customer", "status": "active", **tf}
    customers = list(users_col.find(cust_q, {"name":1,"phone":1,"team_id":1}))
    emp_q     = {"role": "employee", **tf}
    employees = list(users_col.find(emp_q, {"name":1,"phone":1,"team_id":1}))
    # Include enquiries
    enq_q     = {"tag": "form_enquiry", "status": "pending"}
    enquiries = list(enquiries_col.find(enq_q, {"name":1,"phone":1,"plan":1}).limit(500))
    teams     = list(teams_col.find())
    for t in teams:    t["_id"] = str(t["_id"])
    for c in customers: c["_id"] = str(c["_id"]); c["team_id"] = str(c.get("team_id",""))
    for e in employees: e["_id"] = str(e["_id"]); e["team_id"] = str(e.get("team_id",""))
    for enq in enquiries: enq["_id"] = str(enq["_id"])
    return render_template("bulk_whatsapp.html",
        customers=customers, employees=employees, enquiries=enquiries, teams=teams,
        active_page="bulk_wa")


@app.route("/api/admin/bulk_whatsapp/send", methods=["POST"])
@login_required
@role_required("admin", "manager")
def api_bulk_whatsapp_send():
    data      = request.get_json(silent=True) or {}
    target_ids = data.get("ids", [])       # list of user _id strings
    message    = (data.get("message") or "").strip()
    batch_size = int(data.get("batch_size", 10))
    send_all_team = data.get("send_all_team", False)
    team_id       = data.get("team_id", "")

    if not message:
        return jsonify({"success": False, "message": "Message is required."}), 400

    if send_all_team and team_id:
        tf    = _manager_team_filter()
        query = {"team_id": ObjectId(team_id), **tf}
        users = list(users_col.find(query, {"name":1,"phone":1}))
    elif target_ids:
        oids  = [ObjectId(i) for i in target_ids if i]
        users = list(users_col.find({"_id": {"$in": oids}}, {"name":1,"phone":1}))
    else:
        return jsonify({"success": False, "message": "No recipients selected."}), 400

    if not users:
        return jsonify({"success": False, "message": "No valid recipients found."}), 400

    targets = [{"phone": u.get("phone",""), "name": u.get("name","there")} for u in users]
    t = threading.Thread(target=_send_bulk_wa_worker, args=(targets, message, batch_size), daemon=True)
    t.start()

    return jsonify({
        "success": True,
        "message": f"✅ Sending to {len(targets)} recipients in batches of {batch_size}. Runs in background.",
        "count":   len(targets),
    })

@app.route("/api/admin/bulk_whatsapp/send_targets", methods=["POST"])
@login_required
@role_required("admin", "manager")
def api_bulk_whatsapp_send_targets():
    """Send to raw phone+name targets (e.g. enquiries not yet in users_col)."""
    data      = request.get_json(silent=True) or {}
    targets   = data.get("targets", [])   # [{"phone": "...", "name": "..."}]
    message   = (data.get("message") or "").strip()
    batch_size = int(data.get("batch_size", 10))

    if not message:
        return jsonify({"success": False, "message": "Message is required."}), 400
    if not targets or not isinstance(targets, list):
        return jsonify({"success": False, "message": "No targets provided."}), 400

    # Filter out entries with no phone
    valid_targets = [t for t in targets if (t.get("phone") or "").strip()]
    if not valid_targets:
        return jsonify({"success": False, "message": "No valid phone numbers found."}), 400

    t = threading.Thread(
        target=_send_bulk_wa_worker,
        args=(valid_targets, message, batch_size),
        daemon=True
    )
    t.start()

    return jsonify({
        "success": True,
        "message": f"✅ Sending to {len(valid_targets)} enquiries in batches of {batch_size}.",
        "count":   len(valid_targets),
    })


@app.route("/api/admin/bulk_whatsapp/send_to_enquiries", methods=["POST"])
@login_required
@role_required("admin", "manager")
def api_bulk_whatsapp_send_to_enquiries():
    """Send bulk WhatsApp to all pending form_enquiry records."""
    data       = request.get_json(silent=True) or {}
    message    = (data.get("message") or "").strip()
    batch_size = int(data.get("batch_size", 10))
    if not message:
        return jsonify({"success": False, "message": "Message is required."}), 400
    enquiries = list(enquiries_col.find({"tag": "form_enquiry", "status": "pending"}, {"name": 1, "phone": 1}))
    targets   = [{"phone": e.get("phone", ""), "name": e.get("name", "there")} for e in enquiries if e.get("phone")]
    if not targets:
        return jsonify({"success": False, "message": "No pending enquiries with phone numbers found."}), 400
    t = threading.Thread(target=_send_bulk_wa_worker, args=(targets, message, batch_size), daemon=True)
    t.start()
    return jsonify({
        "success": True,
        "message": f"✅ Sending to {len(targets)} pending enquiries in batches of {batch_size}.",
        "count":   len(targets),
    })


# ---------------------------------------------------------------------------
# BACKGROUND SCHEDULER  (rewritten to use a Mongo-backed distributed lock)
# ---------------------------------------------------------------------------
#
# WHY THIS CHANGED:
# The previous version tracked "already ran today" in a plain Python dict
# (_ran_today) that lives ONLY in the memory of a single process. That is
# unsafe for two very common production situations:
#   1. Running with more than one worker process (Gunicorn/uWSGI workers,
#      PM2 cluster mode, etc.) — EVERY worker starts its own copy of this
#      thread, so a job like "payment reminders" fires once PER WORKER,
#      which is exactly what caused customers to get multiple WhatsApp
#      messages instead of one.
#   2. A process restart/redeploy that happens to land inside the same
#      job's time window on the same day — the in-memory dict resets to
#      empty, so the job looks "not yet run" and fires again.
#
# THE FIX:
# Each job attempts to insert a lock document {"job": name, "date": today}
# into a MongoDB collection that has a UNIQUE index on (job, date). Only
# the first process to attempt the insert on a given day succeeds; every
# other process (or every other worker) gets a DuplicateKeyError and simply
# skips. This makes "run at most once per job per day" a guarantee backed
# by the database, not by in-process memory — safe across any number of
# workers and safe across restarts.
# ---------------------------------------------------------------------------

from pymongo.errors import DuplicateKeyError


def _try_acquire_job_lock(job_name: str, today_key: str) -> bool:
    """
    Attempts to atomically claim the right to run `job_name` for `today_key`.
    Returns True if THIS call acquired the lock (i.e. no one else has run
    this job today yet) — the caller should proceed to run the job.
    Returns False if the job has already been claimed (by this process in
    an earlier tick, another worker process, or a previous run before a
    restart) — the caller should skip.
    """
    try:
        scheduler_locks_col.insert_one({
            "job":        job_name,
            "date":       today_key,
            "claimed_at": datetime.now(),
        })
        return True
    except DuplicateKeyError:
        return False
    except Exception as e:
        # If Mongo is briefly unreachable, fail safe by NOT running the job
        # rather than risking duplicate sends.
        print(f"[Scheduler] Lock check failed for {job_name}: {e}")
        return False


def _run_daily_jobs():
    """
    Runs in a background daemon thread.
    Checks every 60 seconds if a scheduled job needs to run.
    Calls functions directly — no HTTP requests / external cron needed.
    This is the only scheduling mechanism used (no cron routes are relied on
    in production — they still exist for manual/debug triggering only).

    Every job below is guarded by _try_acquire_job_lock(), so it is safe to
    run this thread from multiple worker processes at once — only one will
    ever actually execute a given job on a given day.
    """
    print("[Scheduler] ✅ Background scheduler started.")

    while True:
        try:
            now       = datetime.now()
            today_key = now.strftime("%Y-%m-%d")

            # ── 6:00 AM – 6:01 AM — Generate daily deliveries ────────────
            if now.hour == 6 and now.minute < 2:
                if _try_acquire_job_lock("daily_deliveries", today_key):
                    print(f"[Scheduler] ⏰ Running generate_daily_deliveries for {today_key}…")
                    try:
                        _scheduler_generate_daily()
                        print("[Scheduler] ✅ generate_daily_deliveries done.")
                    except Exception as e:
                        print(f"[Scheduler] ❌ generate_daily_deliveries failed: {e}")

            # ── 11:00 AM – 11:01 AM — Payment reminders (day 0 / 1 / 2) ──
            # Sends AT MOST one reminder per customer per day: one on the
            # day they're tagged "new", one the next day, one two days
            # later — then stops (see PAYMENT_REMINDER_OFFSETS + the
            # payment_reminder_days_sent guard inside the job itself).
            if now.hour == 11 and now.minute < 2:
                if _try_acquire_job_lock("payment_reminders", today_key):
                    print(f"[Scheduler] ⏰ Running payment reminders for {today_key}…")
                    try:
                        _run_payment_reminders_job()
                        print("[Scheduler] ✅ payment_reminders done.")
                    except Exception as e:
                        print(f"[Scheduler] ❌ payment_reminders failed: {e}")

            # ── 1:11 PM — Send "Boxes for Tomorrow" to admin WhatsApp ────
            # (Runs during the 13:11–13:12 window so it reliably fires at
            # 1:11 PM even if the loop's 60s tick drifts by a few seconds.)
            if now.hour == 13 and 11 <= now.minute <= 12:
                if _try_acquire_job_lock("boxes_tomorrow_summary", today_key):
                    print(f"[Scheduler] ⏰ Running boxes-tomorrow WhatsApp summary for {today_key}…")
                    try:
                        ok = _send_admin_boxes_tomorrow_summary()
                        print(f"[Scheduler] ✅ boxes_tomorrow_summary done. sent={ok}")
                    except Exception as e:
                        print(f"[Scheduler] ❌ boxes_tomorrow_summary failed: {e}")

            # ── 8:00 PM – 8:01 PM — Check & send invoices due today ──────
            if now.hour == 20 and now.minute < 2:
                if _try_acquire_job_lock("check_invoices", today_key):
                    print(f"[Scheduler] ⏰ Running check_invoice_due for {today_key}…")
                    try:
                        _scheduler_check_invoice_due()
                        print("[Scheduler] ✅ check_invoice_due done.")
                    except Exception as e:
                        print(f"[Scheduler] ❌ check_invoice_due failed: {e}")

        except Exception as e:
            print(f"[Scheduler] ⚠️ Scheduler loop error: {e}")

        time.sleep(60)   # check every minute


def _scheduler_generate_daily():
    """Direct function version of the generate_daily_deliveries route."""
    today    = datetime.now().date()
    date_str = today.strftime("%Y-%m-%d")
    customers = list(users_col.find({"role": "customer", "status": "active"}))
    count = 0
    for cust in customers:
        try:
            plan = plans_col.find_one({"_id": ObjectId(cust["plan_id"])}) if cust.get("plan_id") else None
            if plan:
                if not _day_schedulable_for_plan(plan, today, cust.get("off_days", []), set(cust.get("holidays", []))):
                    continue
            else:
                day_name = today.strftime("%A")
                if day_name in cust.get("off_days", []):
                    continue
                if date_str in cust.get("holidays", []):
                    continue
            exists = deliveries_col.find_one({"customer_id": cust["_id"], "date": date_str})
            if not exists:
                deliveries_col.insert_one({
                    "customer_id":    cust["_id"],
                    "team_id":        cust.get("team_id"),
                    "date":           date_str,
                    "status":         "pending",
                    "assigned_to":    None,
                    "preferred_time": cust.get("preferred_time")
                })
                count += 1
        except Exception as e:
            print(f"[Scheduler] Customer {cust.get('name')} delivery error: {e}")
    print(f"[Scheduler] Generated {count} delivery records for {date_str}")


def _scheduler_check_invoice_due():
    """Direct function version of the check_invoice_due route."""
    today     = date.today()
    customers = list(users_col.find({"role": "customer", "status": "active"}))
    generated = skipped = failed = 0

    for customer in customers:
        try:
            plan = plans_col.find_one({"_id": ObjectId(customer["plan_id"])}) if customer.get("plan_id") else None
            if not plan or int(plan.get("duration_days", 0)) <= 0:
                skipped += 1
                continue

            period_start, period_end = _calculate_invoice_period(customer, plan)
            if period_end != today:
                skipped += 1
                continue

            existing = invoices_col.find_one({
                "customer_id":  customer["_id"],
                "period_start": period_start.strftime("%Y-%m-%d"),
                "period_end":   period_end.strftime("%Y-%m-%d"),
            })
            if existing:
                skipped += 1
                continue

            schedulable, delivered = _billable_days_in_period(customer, period_start, period_end)
            price_total = float(plan.get("price_per_month", plan.get("price_per_day", 0)))
            tax_pct     = float(plan.get("tax_percent", 0))

            subtotal = round((delivered / schedulable) * price_total, 2) if schedulable > 0 else 0.0
            tax      = round(subtotal * tax_pct / 100, 2)
            gross    = subtotal + tax
            disc_pct = float(customer.get("discount_percent", 0) or 0)
            disc_amt = float(customer.get("discount_amount",  0) or 0)
            discount, total = _apply_discount(gross, disc_pct, disc_amt)

            invoice = {
                "customer_id":      customer["_id"],
                "period_start":     period_start.strftime("%Y-%m-%d"),
                "period_end":       period_end.strftime("%Y-%m-%d"),
                "month":            period_end.month,
                "year":             period_end.year,
                "plan_name":        plan.get("name", ""),
                "duration_days":    int(plan.get("duration_days", 0)),
                "schedulable_days": schedulable,
                "billable_days":    delivered,
                "subtotal":         subtotal,
                "tax":              tax,
                "discount":         discount,
                "total":            total,
                "status":           "sent",
                "generated_at":     datetime.now(),
            }
            invoices_col.insert_one(invoice)
            _send_invoice_whatsapp(customer, invoice)
            # NEW: reset payment status for the new billing cycle
            users_col.update_one({"_id": customer["_id"]}, {"$set": {"payment_status": "pending", "payment_partial_amount": 0}})
            generated += 1

        except Exception as e:
            failed += 1
            print(f"[Scheduler] Invoice error for {customer.get('name')}: {e}")

    print(f"[Scheduler] Invoices — generated={generated} skipped={skipped} failed={failed}")


#new route 

@app.route("/api/admin/customer_box_timeline/<customer_id>")
@login_required
@role_required("admin", "manager")
def customer_box_timeline(customer_id):
    """
    Returns a day-by-day box timeline for the customer's current plan period,
    including holiday markers from the customer's holidays[] array.
    """
    try:
        customer = users_col.find_one({"_id": ObjectId(customer_id)})
    except Exception:
        return jsonify({"success": False, "message": "Invalid ID"}), 400
    if not customer:
        return jsonify({"success": False, "message": "Not found"}), 404

    plan = plans_col.find_one({"_id": ObjectId(customer["plan_id"])}) if customer.get("plan_id") else None
    if not plan:
        return jsonify({"success": False, "has_plan": False})

    duration_days = int(plan.get("duration_days", 0))
    start_date    = _parse_start_date(customer)
    if not start_date or duration_days <= 0:
        return jsonify({"success": False, "has_plan": False})

    period_start = start_date
    period_end   = start_date + timedelta(days=duration_days - 1)
    today        = date.today()
    off_days     = customer.get("off_days", [])
    holidays     = set(customer.get("holidays", []))

    # Fetch all deliveries in range
    deliveries_in_period = list(deliveries_col.find({
        "customer_id": customer["_id"],
        "date": {
            "$gte": period_start.strftime("%Y-%m-%d"),
            "$lte": period_end.strftime("%Y-%m-%d")
        }
    }))
    delivery_status_map = {d["date"]: d["status"] for d in deliveries_in_period}

    boxes = []
    current = period_start
    while current <= period_end:
        date_str = current.strftime("%Y-%m-%d")
        day_name = current.strftime("%A")
        d_status = delivery_status_map.get(date_str)
        is_holiday_date = date_str in holidays
        is_off_day      = day_name in off_days

        if d_status == "delivered":
            box_status = "delivered"
        elif d_status == "cancelled_holiday" or is_holiday_date or is_off_day:
            box_status = "holiday"
        elif current < today:
            box_status = "missed"
        elif current == today:
            box_status = "today"
        else:
            box_status = "upcoming"

        boxes.append({
            "date":       date_str,
            "day":        day_name[:3],
            "status":     box_status,
            "is_holiday": is_holiday_date,
            "is_off_day": is_off_day,
        })
        current += timedelta(days=1)

    delivered_count = sum(1 for b in boxes if b["status"] == "delivered")

    return jsonify({
        "success":    True,
        "has_plan":   True,
        "boxes":      boxes,
        "holidays":   sorted(list(holidays)),
        "off_days":   off_days,
        "delivered":  delivered_count,
        "period_start": period_start.strftime("%Y-%m-%d"),
        "period_end":   period_end.strftime("%Y-%m-%d"),
    })


@app.route("/api/admin/customer_plan_history/<customer_id>")
@login_required
@role_required("admin", "manager")
def api_admin_customer_plan_history(customer_id):
    try:
        cust_oid = ObjectId(customer_id)
    except Exception:
        return jsonify({"success": False, "message": "Invalid customer ID"}), 400
    bills = list(manual_bills_col.find({"customer_id": cust_oid}).sort("created_at", -1).limit(24))
    return jsonify({"success": True, "history": [_serialize_manual_bill(b) for b in bills]})


# ---------------------------------------------------------------------------
# ADMIN – MANUAL BILLING (NEW)
# ---------------------------------------------------------------------------
@app.route("/api/admin/customer/<customer_id>/manual_bill", methods=["POST"])
@login_required
@role_required("admin", "manager")
def create_manual_bill(customer_id):
    """
    Creates a manual bill based on admin-marked delivered/holiday dates on a calendar,
    priced against the customer's plan monthly price / plan duration = rate per day.
    Does NOT consider weekends/holidays automatically — purely counts marked boxes.

    Billing duration shown to the customer runs from period_start to the LAST
    delivered date (not the theoretical full period end). After saving, the
    customer's start_date is rolled forward to the day after that last
    delivered date, so the next manual bill / plan cycle picks up correctly
    (e.g. last delivered = 26th → new start_date = 27th).
    """
    data = request.get_json(silent=True) or {}
    period_start_str = data.get("period_start", "").strip()
    period_end_str   = data.get("period_end", "").strip()
    delivered_dates  = list(set(data.get("delivered_dates", [])))
    holiday_dates    = list(set(data.get("holiday_dates", [])))
    bill_month_label = data.get("bill_month_label", "").strip() or "Current Cycle"

    try:
        customer = users_col.find_one({"_id": ObjectId(customer_id), "role": "customer"})
    except Exception:
        return jsonify({"success": False, "message": "Invalid customer ID"}), 400
    if not customer:
        return jsonify({"success": False, "message": "Customer not found"}), 404

    plan = plans_col.find_one({"_id": ObjectId(customer["plan_id"])}) if customer.get("plan_id") else None
    if not plan:
        return jsonify({"success": False, "message": "Customer has no plan assigned"}), 400

    duration_days = int(plan.get("duration_days", 0))
    plan_price    = float(plan.get("price_per_month", plan.get("price_per_day", 0)))
    if duration_days <= 0 or plan_price <= 0 or not period_start_str or not period_end_str:
        return jsonify({"success": False, "message": "Invalid plan or period"}), 400

    working_days = len(delivered_dates)
    leave_days   = len(holiday_dates)
    total_days   = duration_days
    rate_per_day = round(plan_price / duration_days, 2)
    subtotal     = round(working_days * rate_per_day, 2)

    disc_pct = float(customer.get("discount_percent", 0) or 0)
    disc_amt = float(customer.get("discount_amount",  0) or 0)
    discount, total = _apply_discount(subtotal, disc_pct, disc_amt)

    # Billing duration shown to the customer = start date → last delivered
    # date (falls back to period_end if nothing was marked delivered).
    sorted_delivered = sorted(delivered_dates)
    display_end_str  = sorted_delivered[-1] if sorted_delivered else period_end_str

    try:
        period_start_disp = datetime.strptime(period_start_str, "%Y-%m-%d").strftime("%d.%m.%y")
        period_end_disp   = datetime.strptime(display_end_str,  "%Y-%m-%d").strftime("%d.%m.%y")
    except Exception:
        return jsonify({"success": False, "message": "Invalid date format"}), 400

    disc_line = f"• Discount: -₹{discount:.2f}\n" if discount > 0 else ""

    msg = (
        f"🍉 FRUITE DELIGHTS – Monthly Bill ({bill_month_label}) 🍉\n"
        f"Fresh & Healthy Every Day\n\n"
        f"👤 Customer Name: {customer.get('name','')}\n"
        f"📍 Address: {customer.get('address','')}\n\n"
        f"📅 Billing Duration:\n{period_start_disp} to {period_end_disp}\n\n"
        f"📊 Billing Summary:\n"
        f"• Total Days: {total_days}\n"
        f"• Leave Days: {leave_days}\n"
        f"• Working Days: {working_days}\n"
        f"• Rate per Day: ₹{rate_per_day:.2f}\n\n"
        f"💰 Calculation:\n{working_days} × ₹{rate_per_day:.2f} = ₹{subtotal:.2f}\n"
        f"{disc_line}"
        f"✅ Final Payable Amount: ₹{total:.2f}\n\n"
        f"🙏 Thank you for choosing Fruite Delights\n"
        f"Healthy Fruits, Healthy Life 🍓🥝"
    )

    bill_doc = {
        "customer_id":       customer["_id"],
        "customer_name":     customer.get("name", ""),
        "plan_name":         plan.get("name", ""),
        "period_start":      period_start_str,
        "period_end":        period_end_str,
        "display_period_end": display_end_str,
        "bill_month_label":  bill_month_label,
        "total_days":        total_days,
        "leave_days":        leave_days,
        "working_days":      working_days,
        "rate_per_day":      rate_per_day,
        "subtotal":          subtotal,
        "discount":          discount,
        "total":             total,
        "message_text":      msg,
        "delivered_dates":   sorted(delivered_dates),
        "holiday_dates":     sorted(holiday_dates),
        "created_by_id":     session.get("user_id"),
        "created_by_name":   session.get("name"),
        "created_by_role":   session.get("role"),
        "created_at":        datetime.now(),
        "sent":              False,
    }
    result = manual_bills_col.insert_one(bill_doc)

    # Roll the customer's start_date forward to the day after the last
    # delivered date (e.g. last delivered = 26th → new start_date = 27th).
    if sorted_delivered:
        try:
            last_delivered_date = datetime.strptime(sorted_delivered[-1], "%Y-%m-%d").date()
            new_start_date = last_delivered_date + timedelta(days=1)
            users_col.update_one(
                {"_id": customer["_id"]},
                {"$set": {"start_date": datetime.combine(new_start_date, datetime.min.time())}}
            )
        except Exception as e:
            print(f"[ManualBill] Failed to roll start_date for {customer.get('name')}: {e}")

    # Reset payment status whenever a new manual bill is created for the customer
    users_col.update_one({"_id": customer["_id"]}, {"$set": {"payment_status": "pending", "payment_partial_amount": 0}})

    return jsonify({
        "success":      True,
        "bill_id":      str(result.inserted_id),
        "message_text": msg,
        "message":      "Bill generated. Review and send.",
    })


@app.route("/api/admin/manual_bill/<bill_id>/send", methods=["POST"])
@login_required
@role_required("admin", "manager")
def send_manual_bill(bill_id):
    try:
        bill = manual_bills_col.find_one({"_id": ObjectId(bill_id)})
    except Exception:
        return jsonify({"success": False, "message": "Invalid bill ID"}), 400
    if not bill:
        return jsonify({"success": False, "message": "Bill not found"}), 404

    customer = users_col.find_one({"_id": bill["customer_id"]})
    phone = "".join(filter(str.isdigit, str((customer or {}).get("phone", ""))))
    if len(phone) == 10:
        phone = "91" + phone
    if not phone:
        return jsonify({"success": False, "message": "No phone number on file"}), 400

    ok = _safe_send_whatsapp(phone, bill["message_text"], context=f"manual_bill:{bill_id}")
    if ok:
        manual_bills_col.update_one({"_id": bill["_id"]}, {"$set": {"sent": True, "sent_at": datetime.now()}})

    return jsonify({"success": ok, "message": "Sent on WhatsApp." if ok else "WhatsApp delivery failed — check server logs for [WA-FAIL]/[WA-ERROR]."})


@app.route("/api/admin/customer/<customer_id>/manual_bills")
@login_required
@role_required("admin", "manager")
def list_manual_bills(customer_id):
    try:
        bills = list(manual_bills_col.find({"customer_id": ObjectId(customer_id)}).sort("created_at", -1).limit(12))
    except Exception:
        return jsonify({"success": False, "message": "Invalid customer ID"}), 400

    result = []
    for b in bills:
        result.append({
            "id":               str(b["_id"]),
            "bill_month_label": b.get("bill_month_label", ""),
            "period_start":     b.get("period_start", ""),
            "period_end":       b.get("period_end", ""),
            "total":            b.get("total", 0),
            "sent":             b.get("sent", False),
            "created_by_name":  b.get("created_by_name", ""),
        })
    return jsonify({"success": True, "bills": result})


# ---------------------------------------------------------------------------
# WHATSAPP CONNECTIVITY DEBUG (NEW)
# ---------------------------------------------------------------------------
@app.route("/api/system/wa_test_send", methods=["POST"])
@login_required
@role_required("admin")
def wa_test_send():
    """
    Sends a raw test message through the SAME session used for credentials,
    invoices, manual bills, payment reminders, and the admin summary.
    If this fails, the problem is that WhatsApp session (wp.py) — not the
    app code. Check server logs for [WA-FAIL] / [WA-ERROR] right after
    calling this.
    """
    data  = request.get_json(silent=True) or {}
    phone = "".join(filter(str.isdigit, str(data.get("phone", ""))))
    if len(phone) == 10:
        phone = "91" + phone
    if not phone:
        return jsonify({"success": False, "message": "phone required"}), 400
    ok = _safe_send_whatsapp(phone, "✅ Test message from Fruit Delights admin panel.", context="wa_test_send")
    return jsonify({
        "success": ok,
        "message": "Sent." if ok else "Failed — check server logs for [WA-FAIL]/[WA-ERROR] and confirm the WhatsApp session is connected."
    })




# ---------------------------------------------------------------------------
# SUPER ADMIN – FRANCHISE MANAGEMENT (subadmins = franchise owners)
# ---------------------------------------------------------------------------
@app.route("/api/admin/franchises_data")
@login_required
@super_admin_required
def api_franchises_data():
    status_filter = request.args.get("status", "").strip().lower()
    query = {}
    if status_filter in ("active", "pending"):
        query["status"] = status_filter
    franchises = list(franchises_col.find(query))
    result = []
    for fr in franchises:
        fid = fr["_id"]
        subadmin = users_col.find_one({"_id": fr.get("subadmin_id")}) if fr.get("subadmin_id") else None
        result.append({
            "id":               str(fid),
            "business_name":    fr.get("business_name", ""),
            "city":             fr.get("city", ""),
            "address":          fr.get("address", ""),
            "status":           fr.get("status", "pending"),
            "subadmin_id":      str(fr.get("subadmin_id")) if fr.get("subadmin_id") else "",
            "subadmin_name":    subadmin.get("name", "") if subadmin else "",
            "subadmin_email":   subadmin.get("email", "") if subadmin else "",
            "subadmin_phone":   subadmin.get("phone", "") if subadmin else "",
            "manager_count":    users_col.count_documents({"role": "manager",  "franchise_id": fid}),
            "employee_count":   users_col.count_documents({"role": "employee", "franchise_id": fid}),
            "customer_count":   users_col.count_documents({"role": "customer", "franchise_id": fid}),
            "created_at":       fr["created_at"].strftime("%Y-%m-%d") if isinstance(fr.get("created_at"), datetime) else "",
        })
    return jsonify(result)


@app.route("/api/admin/franchise_cities")
@login_required
@super_admin_required
def api_franchise_cities():
    """Case-insensitive de-duplicated city list — 'Delhi' and 'delhi' collapse
    into ONE entry (first-seen casing is kept for display)."""
    cities = franchises_col.distinct("city")
    seen = {}
    for c in cities:
        key = _norm_city_key(c)
        if key and key not in seen:
            seen[key] = c.strip()
    return jsonify(sorted(seen.values(), key=lambda s: s.lower()))


@app.route("/admin/franchises/add", methods=["POST"])
@login_required
@super_admin_required
def add_franchise():
    data          = request.get_json(silent=True) or {}
    business_name = (data.get("business_name") or "").strip()
    owner_name    = (data.get("owner_name") or "").strip()
    phone         = (data.get("phone") or "").strip()
    email_in      = (data.get("email") or "").strip()
    city          = (data.get("city") or "").strip()
    address       = (data.get("address") or "").strip()

    if not business_name or not owner_name or not phone or not city:
        return jsonify({"success": False, "message": "Business name, owner name, phone and city are required."}), 400

    email = email_in or _resolve_customer_email(phone, "")
    if users_col.find_one({"email": email}):
        return jsonify({"success": False, "message": f"User ID '{email}' is already taken."}), 400

    raw_password = secrets.token_urlsafe(8)

    fr_result = franchises_col.insert_one({
        "business_name": business_name,
        "city":          city,
        "address":       address,
        "status":        "pending",
        "subadmin_id":   None,
        "created_at":    datetime.now(),
    })
    fid = fr_result.inserted_id

    sub_result = users_col.insert_one({
        "name":         owner_name,
        "email":        email,
        "phone":        phone,
        "role":         "subadmin",
        "password":     generate_password_hash(raw_password),
        "franchise_id": fid,
        "created_at":   datetime.now(),
    })
    franchises_col.update_one({"_id": fid}, {"$set": {"subadmin_id": sub_result.inserted_id}})

    send_franchise_credentials_whatsapp(email, raw_password, owner_name, phone, business_name, city, address)

    return jsonify({
        "success":      True,
        "franchise_id": str(fid),
        "login_id":     email,
        "password":     raw_password,
        "message":      f"✅ Franchise '{business_name}' created. Credentials sent on WhatsApp.",
    })


@app.route("/admin/franchises/<franchise_id>/status", methods=["POST"])
@login_required
@super_admin_required
def update_franchise_status(franchise_id):
    data   = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip().lower()
    if status not in ("active", "pending"):
        return jsonify({"success": False, "message": "Invalid status."}), 400
    try:
        franchises_col.update_one({"_id": ObjectId(franchise_id)}, {"$set": {"status": status}})
    except Exception:
        return jsonify({"success": False, "message": "Invalid franchise ID."}), 400
    return jsonify({"success": True, "status": status})


@app.route("/admin/franchises/<franchise_id>/reset-credentials", methods=["POST"])
@login_required
@super_admin_required
def reset_franchise_credentials(franchise_id):
    try:
        fr = franchises_col.find_one({"_id": ObjectId(franchise_id)})
    except Exception:
        return jsonify({"success": False, "message": "Invalid franchise ID."}), 400
    if not fr or not fr.get("subadmin_id"):
        return jsonify({"success": False, "message": "Franchise owner not found."}), 404
    subadmin = users_col.find_one({"_id": fr["subadmin_id"]})
    if not subadmin:
        return jsonify({"success": False, "message": "Owner account not found."}), 404

    new_password = secrets.token_urlsafe(8)
    users_col.update_one({"_id": subadmin["_id"]}, {"$set": {"password": generate_password_hash(new_password)}})
    phone = "".join(filter(str.isdigit, str(subadmin.get("phone", ""))))
    if len(phone) == 10:
        phone = "91" + phone
    name, login_id = subadmin.get("name", ""), subadmin.get("email", "")
    msg = (
        f"🍓 Fruit Delights – Franchise Password Reset\n\n"
        f"Hello {name},\n\nYour franchise login password has been reset.\n\n"
        f"Login ID: {login_id}\nNew Password: {new_password}\n\n"
        f"Login:\nhttps://fruitedelights.com/login\n\n"
        f"If you did not request this, contact HQ support."
    )
    wa_sent = _safe_send_whatsapp(phone, msg, context="reset_franchise") if phone else False
    return jsonify({
        "success": True, "wa_sent": wa_sent, "name": name,
        "login_id": login_id, "new_password": new_password,
        "message": f"Password reset for {name}." + (" Sent on WhatsApp." if wa_sent else " WhatsApp delivery failed."),
    })


# ---------------------------------------------------------------------------
# CRON ROUTES (kept for manual/debug triggering only — production scheduling
# runs via the background thread started at the bottom of this file, since
# this app runs on a Docker VPS without external cron access).
# NOTE: these manual routes also go through the same DB lock, so triggering
# one manually on the same day a scheduled run already happened will report
# success/failure honestly instead of double-sending.
# ---------------------------------------------------------------------------
@app.route("/api/system/send_payment_reminders")
def send_payment_reminders():
    token = request.args.get("token")
    if token != os.getenv("CRON_SECRET"):
        return jsonify({"error": "Unauthorized"}), 401
    sent = _run_payment_reminders_job()
    return jsonify({"success": True, "sent": sent})


@app.route("/api/system/send_boxes_tomorrow_summary")
def send_boxes_tomorrow_summary():
    token = request.args.get("token")
    if token != os.getenv("CRON_SECRET"):
        return jsonify({"error": "Unauthorized"}), 401
    ok = _send_admin_boxes_tomorrow_summary()
    return jsonify({"success": bool(ok)})


@app.route("/api/employee/holiday_customers")
@login_required
@role_required("employee")
def employee_holiday_customers():
    employee = users_col.find_one({"_id": ObjectId(session["user_id"])})
    if not employee or "team_id" not in employee:
        return jsonify({"success": False, "message": "Not assigned to a team"}), 400

    today     = datetime.now().date()
    today_str = today.strftime("%Y-%m-%d")
    day_name  = today.strftime("%A")

    customers = list(users_col.find({
        "role":    "customer",
        "team_id": employee["team_id"],
        "status":  "active",
    }))

    result = []
    for cust in customers:
        off_days = cust.get("off_days", [])
        holidays = set(cust.get("holidays", []))
        plan = plans_col.find_one({"_id": ObjectId(cust["plan_id"])}) if cust.get("plan_id") else None

        if plan:
            is_holiday_today = not _day_schedulable_for_plan(plan, today, off_days, holidays)
        else:
            is_holiday_today = (day_name in off_days) or (today_str in holidays)

        if not is_holiday_today:
            continue

        plan_name  = plan.get("name", "") if plan else cust.get("sample", "")
        today_item = None
        if plan:
            alt_items = plan.get("alternate_items", [])
            if alt_items and len(alt_items) >= 2:
                cust_alt   = cust.get("alt_options", [])
                today_item = _get_customer_item_for_date(cust, today) if cust_alt else _get_alternate_item_for_date(plan, today)

        result.append({
            "id":         str(cust["_id"]),
            "name":       cust.get("name", ""),
            "phone":      cust.get("phone", ""),
            "address":    cust.get("address", ""),
            "plan_name":  plan_name,
            "today_item": today_item,
            "reason":     "holiday" if (today_str in holidays) else "off_day",
        })

    return jsonify({"success": True, "count": len(result), "customers": result, "date": today_str})


@app.route("/api/admin/bulk_assign_manager", methods=["POST"])
@login_required
def bulk_assign_manager_customers():
    """
    Assigns ALL of the caller's customers (scoped to their own franchise/HQ)
    to a single manager in one shot. Restricted to admin & subadmin only —
    managers themselves cannot reassign a whole customer base.

    Scoping:
      - admin (HQ)   -> franchise_id is None  -> only HQ customers, only HQ managers
      - subadmin     -> franchise_id is theirs -> only their franchise's customers/managers
    """
    if session.get("role") not in ("admin", "subadmin"):
        return jsonify({"success": False, "message": "Unauthorized."}), 403

    data = request.get_json(silent=True) or {}
    manager_id = data.get("manager_id")
    if not manager_id:
        return jsonify({"success": False, "message": "manager_id is required."}), 400

    try:
        manager = users_col.find_one({"_id": ObjectId(manager_id), "role": "manager"})
    except Exception:
        return jsonify({"success": False, "message": "Invalid manager_id."}), 400
    if not manager:
        return jsonify({"success": False, "message": "Manager not found."}), 404

    caller_franchise_id = _current_franchise_id()   # None for admin, ObjectId for subadmin
    manager_franchise_id = manager.get("franchise_id")

    if manager_franchise_id != caller_franchise_id:
        return jsonify({"success": False, "message": "That manager is not part of your franchise/HQ."}), 403

    query = {"role": "customer", "franchise_id": caller_franchise_id}
    result = users_col.update_many(query, {"$set": {"assigned_manager_id": manager["_id"]}})

    return jsonify({
        "success":  True,
        "matched":  result.matched_count,
        "modified": result.modified_count,
        "message":  f"Assigned {result.matched_count} customer(s) to {manager.get('name','')}."
    })


# ── Start the scheduler thread ─────────────────────────────────────────────
# Guard prevents double-start when Flask debug reloader spawns a child process.
# NOTE: this guard only helps with Flask's *dev* auto-reloader. It does NOT
# protect against multiple Gunicorn/uWSGI worker processes in production —
# that protection now comes from the Mongo-backed job lock above
# (_try_acquire_job_lock), which is why jobs are safe even if this thread
# starts in several worker processes at once.
if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
    _sched_thread = threading.Thread(target=_run_daily_jobs, daemon=True)
    _sched_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5757)