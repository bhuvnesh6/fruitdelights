from flask import Flask, render_template, request, redirect, session, url_for, flash, jsonify
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




load_dotenv()


cloudinary.config(
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key    = os.getenv("CLOUDINARY_API_KEY"),
    api_secret = os.getenv("CLOUDINARY_API_SECRET"),
    secure     = True,
)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key-change-in-production")

app.config["CLOUDINARY_CLOUD_NAME"]    = os.getenv("CLOUDINARY_CLOUD_NAME", "")
app.config["CLOUDINARY_UPLOAD_PRESET"] = os.getenv("CLOUDINARY_UPLOAD_PRESET", "")

# Mongo Config
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME   = os.getenv("DB_NAME")
client    = MongoClient(MONGO_URI)
db        = client[DB_NAME]


# Collections
users_col      = db.users
teams_col      = db.teams
plans_col      = db.plans
deliveries_col = db.deliveries
invoices_col   = db.invoices
enquiries_col  = db.enquiries
wa_inbox_col   = db.wa_inbox          # ← NEW: WhatsApp conversation store


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


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if session.get("role") not in roles:
                flash("Unauthorized access.", "danger")
                return redirect(url_for("home"))
            return f(*args, **kwargs)
        return decorated_function
    return decorator


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def send_credentials_whatsapp(email, password, name, phone):
    """Send login credentials to customer's WhatsApp."""
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
    send_whatsapp(phone, msg)


def _generate_customer_login_id(name: str) -> str:
    parts = re.sub(r'[^a-zA-Z0-9 ]', '', name).strip().split()
    base  = parts[0][:8].lower() if parts else "user"
    if len(parts) > 1:
        base += parts[1][:2].lower()
    suffix    = secrets.token_hex(2)
    candidate = f"{base}{suffix}@fruitedelights.com"
    while users_col.find_one({"email": candidate}):
        candidate = f"{base}{secrets.token_hex(2)}@fruitedelights.com"
    return candidate


def _resolve_customer_email(phone, existing_email=""):
    email = (existing_email or "").strip()
    if email:
        return email
    safe_phone = "".join(filter(str.isdigit, phone or "unknown"))
    generated  = f"{safe_phone}@fruitedelights.local"
    if users_col.find_one({"email": generated}):
        generated = f"{safe_phone}_{secrets.token_hex(3)}@fruitedelights.local"
    return generated


def _manager_team_filter():
    """
    Returns a dict with team_id restriction if current session user is a manager.
    Returns {} (no restriction) if admin.
    """
    if session.get("role") == "manager":
        user = users_col.find_one({"_id": ObjectId(session["user_id"])})
        tid  = user.get("team_id") if user else None
        if tid:
            return {"team_id": ObjectId(tid)}
        return {"team_id": None}   # manager with no team sees nothing
    return {}

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
    period_end   = start + timedelta(days=duration_days - 1)
    return period_start, period_end


def _billable_days_in_period(customer, period_start, period_end):
    off_days  = customer.get("off_days", [])
    holidays  = set(customer.get("holidays", []))
    schedulable = 0
    delivered   = 0
    current     = period_start
    while current <= period_end:
        day_name = current.strftime("%A")
        date_str = current.strftime("%Y-%m-%d")
        if day_name not in off_days and date_str not in holidays:
            schedulable += 1
            delivery = deliveries_col.find_one({
                "customer_id": ObjectId(customer["_id"]),
                "date":        date_str,
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
    off_days     = customer.get("off_days", [])
    holidays     = set(customer.get("holidays", []))
    billable_days = 0
    current = period_start
    while current <= period_end:
        if current.strftime("%A") not in off_days and current.strftime("%Y-%m-%d") not in holidays:
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
    send_whatsapp(phone, msg)


# ---------------------------------------------------------------------------
# WA INBOX HELPERS
# ---------------------------------------------------------------------------
INBOX_RETENTION_DAYS = 14   # messages older than this are auto-deleted


def _wa_thread_id(enquiry_id: str) -> str:
    """Canonical thread key for one enquiry conversation."""
    return str(enquiry_id)


def _wa_prune_old_messages(enquiry_id: str):
    """Delete individual messages older than INBOX_RETENTION_DAYS from the thread."""
    cutoff = datetime.utcnow() - timedelta(days=INBOX_RETENTION_DAYS)
    wa_inbox_col.update_one(
        {"enquiry_id": enquiry_id},
        {"$pull": {"messages": {"ts": {"$lt": cutoff}}}}
    )


def _wa_append_message(enquiry_id: str, direction: str, body: str,
                        whatsapp_id: str = "", phone: str = "",
                        push_name: str = "", message_id: str = ""):
    """
    Append one message to the thread document for this enquiry.
    direction: 'in' (from user) | 'out' (sent by admin)
    Creates the document if it doesn't exist.
    """
    now = datetime.utcnow()
    msg = {
        "direction":  direction,
        "body":       body,
        "ts":         now,
        "message_id": message_id,
    }
    wa_inbox_col.update_one(
        {"enquiry_id": enquiry_id},
        {
            "$push":    {"messages": msg},
            "$set":     {
                "enquiry_id":   enquiry_id,
                "whatsapp_id":  whatsapp_id or None,
                "phone":        phone or None,
                "push_name":    push_name or None,
                "last_updated": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True
    )
    _wa_prune_old_messages(enquiry_id)



#plan helpers 

def _get_alternate_item_for_date(plan, target_date):
    """
    Returns the alternate item label for a given date.
    Uses the plan's start_date as anchor (day 0 = first item).
    Falls back to a reference date of 2024-01-01 if no anchor.
    """
    alt_items = plan.get("alternate_items", [])
    if not alt_items or len(alt_items) < 2:
        return None   # not an alternate plan
 
    # Use a fixed epoch so alternation is consistent across all customers
    epoch = date(2024, 1, 1)
    delta = (target_date - epoch).days
    idx   = delta % len(alt_items)
    return alt_items[idx]


# ---------------------------------------------------------------------------
# ROUTES – core
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    if "user_id" in session:
        role = session.get("role")
        if role in ["admin", "manager"]:
            return redirect(url_for("dashboard_admin"))
        elif role == "employee":
            return redirect(url_for("dashboard_employee"))
        elif role == "customer":
            return redirect(url_for("dashboard_customer"))
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email    = request.form.get("email")
        password = request.form.get("password")
        user     = users_col.find_one({"email": email})
        if user and check_password_hash(user["password"], password):
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
    send_whatsapp(wa_phone, msg)
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
 
    # Build customer query with optional team restriction
    cust_q = {"role": "customer"}
    if tf:
        cust_q.update(tf)
 
    stats = {
        "total_customers":    users_col.count_documents(cust_q),
        "total_employees":    users_col.count_documents({**{"role": "employee"}, **tf}),
        "pending_deliveries": deliveries_col.count_documents({**{"status": "pending",           "date": today}, **tf}),
        "delivered_today":    deliveries_col.count_documents({**{"status": "delivered",         "date": today}, **tf}),
        "on_holiday_today":   deliveries_col.count_documents({**{"status": "cancelled_holiday", "date": today}, **tf}),
        "active_teams":       teams_col.count_documents({}),
    }
 
    # Admin sees all teams/plans; manager sees only their team
    if session.get("role") == "manager":
        user = users_col.find_one({"_id": ObjectId(session["user_id"])})
        assigned_tid = user.get("team_id") if user else None
        teams = [teams_col.find_one({"_id": assigned_tid})] if assigned_tid else []
        teams = [t for t in teams if t]
    else:
        teams = list(teams_col.find())
 
    plans = list(plans_col.find())
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
    result = []
    for c in customers:
        team = team_map.get(str(c.get("team_id", "")), {})
        plan = plan_map.get(str(c.get("plan_id", "")), {})
        start_date = c.get("start_date")
        if isinstance(start_date, datetime):
            start_date = start_date.strftime("%Y-%m-%d")
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
    # build a set of enquiry IDs that have inbox messages
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
    results = []
    for i in range(days - 1, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        results.append({
            "date":      d,
            "delivered": deliveries_col.count_documents({"status": "delivered",         "date": d}),
            "pending":   deliveries_col.count_documents({"status": "pending",           "date": d}),
            "holiday":   deliveries_col.count_documents({"status": "cancelled_holiday", "date": d}),
        })
    today     = datetime.now().strftime("%Y-%m-%d")
    teams     = list(teams_col.find())
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
    plans = list(plans_col.find())
    return render_template("plans.html", plans=plans, active_page="plans")


@app.route("/admin/plans/add", methods=["POST"])
@login_required
@role_required("admin", "manager")
def add_plan():
    alt_raw  = request.form.get("alternate_items", "").strip()
    alt_list = [x.strip() for x in alt_raw.split(",") if x.strip()] if alt_raw else []
    plans_col.insert_one({
        "name":             request.form.get("name"),
        "description":      request.form.get("description"),
        "price_per_month":  float(request.form.get("price_per_month", 0)),
        "duration_days":    int(request.form.get("duration_days", 0)),
        "tax_percent":      float(request.form.get("tax_percent", 0)),
        "alternate_items":  alt_list,   # [] means normal plan
        "created_at":       datetime.now()
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
    query       = {}
    if city_filter:
        query["city"] = {"$regex": city_filter, "$options": "i"}
    teams  = list(teams_col.find(query))
    for t in teams:
        t["employee_count"] = users_col.count_documents({"role": "employee", "team_id": t["_id"]})
    cities = teams_col.distinct("city")
    return render_template("teams.html", teams=teams, cities=cities,
                           city_filter=city_filter, active_page="teams")


@app.route("/admin/teams/add", methods=["POST"])
@login_required
@role_required("admin", "manager")
def add_team():
    teams_col.insert_one({
        "name":       request.form.get("name"),
        "area":       request.form.get("area"),
        "city":       request.form.get("city"),
        "created_at": datetime.now()
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


# ---------------------------------------------------------------------------
# ADMIN – Reset employee credentials  ← NEW
# ---------------------------------------------------------------------------
@app.route("/admin/employees/reset-credentials/<employee_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def reset_employee_credentials(employee_id):
    employee = users_col.find_one({"_id": ObjectId(employee_id), "role": "employee"})
    if not employee:
        return jsonify({"success": False, "message": "Employee not found."}), 404

    new_password = secrets.token_urlsafe(8)
    users_col.update_one(
        {"_id": ObjectId(employee_id)},
        {"$set": {"password": generate_password_hash(new_password)}}
    )

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
        wa_sent = send_whatsapp(phone, msg)

    return jsonify({
        "success":  True,
        "wa_sent":  wa_sent,
        "name":     name,
        "message":  f"Credentials reset for {name}." + (" Sent on WhatsApp." if wa_sent else " WhatsApp delivery failed."),
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
        "name":       name,
        "email":      email,
        "phone":      phone,
        "role":       role,
        "password":   generate_password_hash(raw_password),
        "created_at": datetime.now()
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
    if role == "customer":
        existing       = users_col.find_one({"_id": ObjectId(user_id)}, {"email": 1})
        existing_email = (existing or {}).get("email", "")
        form_email     = request.form.get("email", "").strip()
        email          = form_email if form_email else existing_email
    else:
        email = request.form.get("email", "").strip()
    update = {
        "name":  request.form.get("name"),
        "email": email,
        "phone": phone,
    }
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
        update.update({
            "plan_id":          plan["_id"] if plan else None,
            "address":          request.form.get("address"),
            "preferred_time":   request.form.get("preferred_time"),
            "off_days":         request.form.getlist("off_days"),
            "start_date":       start_date,
            "discount_percent": float(request.form.get("discount_percent", 0) or 0),
            "discount_amount":  float(request.form.get("discount_amount",  0) or 0),
            "status":           request.form.get("customer_status", "active"),
        })
    users_col.update_one({"_id": ObjectId(user_id)}, {"$set": update})
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
    send_whatsapp(wa_phone, msg)
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
# WHATSAPP INBOX – Send message from admin  ← NEW
# ---------------------------------------------------------------------------
@app.route("/api/admin/wa_send", methods=["POST"])
@login_required
@role_required("admin", "manager")
def wa_send():
    """
    Admin sends  WhatsApp message to an enquiry contact.
    Body JSON: { "enquiry_id": "...", "message": "..." }
    Uses whatsapp_id if stored, else falls back to phone number.
    """
    data       = request.get_json(silent=True) or {}
    enquiry_id = data.get("enquiry_id", "").strip()
    message    = data.get("message", "").strip()

    if not enquiry_id or not message:
        return jsonify({"success": False, "message": "enquiry_id and message are required."}), 400

    enq = enquiries_col.find_one({"_id": ObjectId(enquiry_id)})
    if not enq:
        return jsonify({"success": False, "message": "Enquiry not found."}), 404

    # Prefer whatsapp_id, fall back to phone
    phone = "".join(filter(str.isdigit, str(enq.get("phone", ""))))

    if len(phone) == 10:
       phone = "91" + phone

    if not phone:
     return jsonify({
        "success": False,
        "message": "No phone number found on this enquiry."
     }), 400

    ok = send_whatsapp_msg(phone, message)

    if ok:
        # Store outgoing in inbox
        _wa_append_message(
            enquiry_id=enquiry_id,
            direction="out",
            body=message,
            whatsapp_id=identifier,
            phone=enq.get("phone", ""),
        )

    return jsonify({"success": ok, "message": "Sent." if ok else "WhatsApp delivery failed."})


# ---------------------------------------------------------------------------
# WHATSAPP INBOX – Get thread for one enquiry  ← NEW
# ---------------------------------------------------------------------------
@app.route("/api/admin/wa_thread/<enquiry_id>")
@login_required
@role_required("admin", "manager")
def wa_thread(enquiry_id):
    doc = wa_inbox_col.find_one({"enquiry_id": enquiry_id})

    # If not found, try matching by unknown bucket using the enquiry's whatsapp_id
    if not doc:
        enq = enquiries_col.find_one({"_id": ObjectId(enquiry_id)})
        if enq:
            wa_id = enq.get("whatsapp_id", "")
            phone = "".join(filter(str.isdigit, str(enq.get("phone", ""))))
            raw_digits = wa_id.split("@")[0] if wa_id else phone
            if raw_digits:
                doc = wa_inbox_col.find_one({"enquiry_id": f"unknown_{raw_digits}"})
            # If found, re-link it to the real enquiry_id for future
            if doc:
                wa_inbox_col.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"enquiry_id": enquiry_id}}
                )

    if not doc:
        return jsonify({"messages": [], "push_name": "", "phone": "", "whatsapp_id": ""})

    messages = []
    for m in doc.get("messages", []):
        messages.append({
            "direction":  m.get("direction", "in"),
            "body":       m.get("body", ""),
            "ts":         m["ts"].isoformat() if isinstance(m.get("ts"), datetime) else str(m.get("ts", "")),
            "message_id": m.get("message_id", ""),
        })
    return jsonify({
        "messages":    messages,
        "push_name":   doc.get("push_name", ""),
        "phone":       doc.get("phone", ""),
        "whatsapp_id": doc.get("whatsapp_id", ""),
    })

# ---------------------------------------------------------------------------
# WHATSAPP INBOX – Incoming webhook (PUBLIC – called by n8n)  ← NEW
# ---------------------------------------------------------------------------
@app.route("/api/webhook/wa_incoming", methods=["POST"])
def wa_incoming_webhook():
    """
    n8n calls this endpoint when a WhatsApp message arrives.
    Expected JSON body (from your webhook output):
    {
        "instanceName": "Fruit_Delights",
        "whatsappId":   "63582796566668@lid",
        "phoneNumber":  "63582796566668",       ← may be empty string
        "body":         "message text",
        "pushName":     "John",
        "timestamp":    1780718285,
        "messageId":    "false_63582796566668@lid_3EB0139F5A8B2FA8FA53",
        "enquiry_id":   "6649abc..."             ← optional; if n8n can resolve it
    }
    If enquiry_id is not supplied, we match by whatsappId or phone against the
    enquiries collection (field: whatsapp_id or phone).
    """
    data = request.get_json(silent=True) or {}

    whatsapp_id = (data.get("whatsappId") or "").strip()
    phone_raw   = (data.get("phoneNumber") or "").strip()
    body        = (data.get("body") or "").strip()
    push_name   = (data.get("pushName") or "").strip()
    message_id  = (data.get("messageId") or "").strip()
    timestamp   = data.get("timestamp")

    if not body:
        return jsonify({"status": "ignored", "reason": "empty body"}), 200

    # Normalise phone
    phone = "".join(filter(str.isdigit, phone_raw))
    if len(phone) == 12 and phone.startswith("91"):
        phone_10 = phone[2:]
    elif len(phone) == 10:
        phone_10 = phone
        phone    = "91" + phone
    else:
        phone_10 = phone

    # ── Try to find the enquiry ──
    enquiry_id_str = (data.get("enquiry_id") or "").strip()
    enq = None

    if enquiry_id_str:
        try:
            enq = enquiries_col.find_one({"_id": ObjectId(enquiry_id_str)})
        except Exception:
            pass

    if not enq and whatsapp_id:
        enq = enquiries_col.find_one({"whatsapp_id": whatsapp_id})

    if not enq and phone:
        enq = enquiries_col.find_one({"phone": {"$in": [phone, phone_10, phone_raw]}})

    if not enq:
        # Save under a "unknown" bucket so we can still see the message
        raw_digits = whatsapp_id.split("@")[0] if whatsapp_id else phone
        bucket_id = f"unknown_{raw_digits}"
        
        _wa_append_message(
            enquiry_id=bucket_id,
            direction="in",
            body=body,
            whatsapp_id=whatsapp_id,
            phone=phone,
            push_name=push_name,
            message_id=message_id,
        )
        return jsonify({"status": "saved", "matched": False, "bucket": bucket_id}), 200

    enq_id = str(enq["_id"])

    # Store whatsapp_id on enquiry if not already there
    if whatsapp_id and not enq.get("whatsapp_id"):
        enquiries_col.update_one(
            {"_id": enq["_id"]},
            {"$set": {"whatsapp_id": whatsapp_id}}
        )

    _wa_append_message(
        enquiry_id=enq_id,
        direction="in",
        body=body,
        whatsapp_id=whatsapp_id,
        phone=phone,
        push_name=push_name,
        message_id=message_id,
    )

    return jsonify({"status": "saved", "matched": True, "enquiry_id": enq_id}), 200


@app.route("/api/admin/enquiry_count")
@login_required
@role_required("admin", "manager")
def enquiry_count_dup():
    # duplicate guard – remove if original is above
    pass
# Remove above stub – already defined. Flask uses first definition.


# ═══════════════════════════════════════════════════════════════════════════
# ADD THIS ROUTE TO app.py (after the /admin/enquiries/delete route)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/admin/bulk_enquiries_import", methods=["POST"])
@login_required
@role_required("admin", "manager")
def bulk_enquiries_import():
    """
    Bulk import enquiries from frontend spreadsheet-like data.
    Expected JSON body: { "enquiries": [ { "name": "...", "phone": "...", ... }, ... ] }
    """
    data = request.get_json(silent=True) or {}
    enquiries_list = data.get("enquiries", [])
    
    if not enquiries_list:
        return jsonify({"success": False, "message": "No enquiries provided."}), 400
    
    if not isinstance(enquiries_list, list):
        return jsonify({"success": False, "message": "enquiries must be a list."}), 400
    
    inserted = 0
    skipped = 0
    errors = []
    
    plans = list(plans_col.find({}))
    plan_map = {p.get("name", ""): p for p in plans}
    
    for idx, enq_data in enumerate(enquiries_list):
        try:
            # Validate required fields
            name = (enq_data.get("name") or "").strip()
            phone = (enq_data.get("phone") or "").strip()
            
            if not name or not phone:
                skipped += 1
                errors.append(f"Row {idx + 1}: Name and phone are required.")
                continue
            
            # Check for duplicates
            existing = enquiries_col.find_one({"phone": phone, "tag": "form_enquiry"})
            if existing:
                skipped += 1
                errors.append(f"Row {idx + 1}: Phone {phone} already exists.")
                continue
            
            # Extract fields
            plan_name = (enq_data.get("plan") or "").strip()
            delivery_time = (enq_data.get("delivery_time") or "").strip()
            start_date = (enq_data.get("start_date") or "").strip()
            address = (enq_data.get("address") or "").strip()
            payment_method = (enq_data.get("payment_method") or "upi").strip().lower()
            status = (enq_data.get("status") or "pending").strip().lower()
            
            # Validate status
            if status not in ["pending", "approved", "converted"]:
                status = "pending"
            
            # Validate payment method
            if payment_method not in ["upi", "cod"]:
                payment_method = "upi"
            
            # Insert the enquiry
            enquiry_doc = {
                "name": name,
                "phone": phone,
                "address": address,
                "delivery_time": delivery_time,
                "start_date": start_date,
                "plan": plan_name,
                "payment_method": payment_method,
                "status": status,
                "source": "admin_bulk_import",
                "tag": "form_enquiry",
                "created_at": datetime.now(),
            }
            
            result = enquiries_col.insert_one(enquiry_doc)
            inserted += 1
            
        except Exception as e:
            skipped += 1
            errors.append(f"Row {idx + 1}: {str(e)}")
    
    return jsonify({
        "success": True,
        "inserted": inserted,
        "skipped": skipped,
        "total": len(enquiries_list),
        "errors": errors[:10],  # Limit to first 10 errors
        "message": f"✅ Imported {inserted} enquiries. {skipped} skipped."
    })


@app.route("/api/admin/enquiries_export")
@login_required
@role_required("admin", "manager")
def enquiries_export():
    """
    Export all enquiries as JSON for download (template for bulk import).
    """
    enquiries = list(enquiries_col.find({"tag": "form_enquiry"}).limit(1000))
    
    data = []
    for e in enquiries:
        data.append({
            "name": e.get("name", ""),
            "phone": e.get("phone", ""),
            "address": e.get("address", ""),
            "plan": e.get("plan", ""),
            "delivery_time": e.get("delivery_time", ""),
            "start_date": e.get("start_date", ""),
            "payment_method": e.get("payment_method", "upi"),
            "status": e.get("status", "pending"),
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
    day_name   = datetime.now().strftime("%A")
    emp_id_str = str(employee["_id"])
 
    existing_deliveries = list(deliveries_col.find({
        "team_id": employee["team_id"],
        "date":    today_str
    }))
    delivery_map = {str(d["customer_id"]): d for d in existing_deliveries}
 
    # ── PATCH: only active customers (status == "active" OR status field absent) ──
    customers = list(users_col.find({
        "role":    "customer",
        "team_id": employee["team_id"],
        "status":  "active",          # ← was {"$ne": "inactive"}, now strict active-only
    }))
 
    final_deliveries = []
    for cust in customers:
        cust_id_str = str(cust["_id"])
 
        # Skip customer's weekly off-day
        if day_name in cust.get("off_days", []):
            continue
 
        # Skip customer's holiday for today
        if today_str in cust.get("holidays", []):
            continue
 
        if cust_id_str in delivery_map:
            delivery = delivery_map[cust_id_str]
            # Skip if already marked cancelled_holiday
            if delivery.get("status") == "cancelled_holiday":
                continue
            delivery["customer"] = [cust]
            final_deliveries.append(delivery)
        else:
            virtual_delivery = {
                "_id":            f"virtual_{cust_id_str}",
                "customer_id":    cust["_id"],
                "team_id":        employee["team_id"],
                "date":           today_str,
                "status":         "pending",
                "assigned_to":    None,
                "preferred_time": cust.get("preferred_time", "Anytime"),
                "proof_photo_url": cust.get("latest_proof_photo_url") or None,     # new field default
                "customer":       [cust]
            }
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


# ---------------------------------------------------------------------------
# CRON – Daily deliveries
# ---------------------------------------------------------------------------
@app.route("/api/system/generate_daily", methods=["GET"])
def generate_daily_deliveries():
    today    = datetime.now()
    date_str = today.strftime("%Y-%m-%d")
    day_name = today.strftime("%A")
    customers = users_col.find({"role": "customer"})
    count = 0
    for cust in customers:
        if day_name in cust.get("off_days", []):
            continue
        if date_str in cust.get("holidays", []):
            continue
        exists = deliveries_col.find_one({"customer_id": cust["_id"], "date": date_str})
        if not exists:
            deliveries_col.insert_one({
                "customer_id":    cust["_id"],
                "team_id":        cust["team_id"],
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
# ADMIN – Boxes needed today
# ---------------------------------------------------------------------------
@app.route("/api/admin/boxes_needed")
@login_required
@role_required("admin", "manager")
def api_boxes_needed():
    now      = datetime.now()
    tf       = _manager_team_filter()
 
    # ── After 1pm → show next day's boxes ──────────────────
    if now.hour >= 13:
        target_date = (now + timedelta(days=1)).date()
        is_next_day = True
    else:
        target_date = now.date()
        is_next_day = False
 
    today_str  = now.strftime("%Y-%m-%d")        # always today for holiday checks
    target_str = target_date.strftime("%Y-%m-%d")
    day_name   = target_date.strftime("%A")
 
    plans  = list(plans_col.find())
    teams  = list(teams_col.find())
 
    base_cust_q = {
        "role":    "customer",
        "status":  "active",
        "plan_id": {"$ne": None},
        "$or": [
            {"sample": {"$exists": False}},
            {"sample": None},
            {"sample": ""},
        ]
    }
    if tf:
        base_cust_q.update(tf)
 
    active_customers = list(users_col.find(base_cust_q))
 
    sample_q = {
        "role":   "customer",
        "status": "active",
        "sample": {"$exists": True, "$nin": [None, ""]},
    }
    if tf:
        sample_q.update(tf)
    sample_customers = list(users_col.find(sample_q))
 
    team_map = {str(t["_id"]): t for t in teams}
    result   = []
 
    for plan in plans:
        plan_id   = str(plan["_id"])
        plan_name = plan.get("name", "")
        alt_items = plan.get("alternate_items", [])
 
        # Determine today's alternate item label
        alt_label = _get_alternate_item_for_date(plan, target_date)
 
        team_breakdown = {}
        active_count   = 0
 
        for c in active_customers:
            if str(c.get("plan_id", "")) != plan_id:
                continue
            if day_name in c.get("off_days", []):
                continue
            if target_str in c.get("holidays", []):
                continue
            active_count += 1
            tid = str(c.get("team_id", ""))
            if tid not in team_breakdown:
                team_obj = team_map.get(tid, {})
                team_breakdown[tid] = {
                    "team_id":   tid,
                    "team_name": team_obj.get("name", "—"),
                    "team_area": team_obj.get("area", ""),
                    "count":     0,
                }
            team_breakdown[tid]["count"] += 1
 
        matched_sample_count = 0
        for c in sample_customers:
            if str(c.get("plan_id", "")) == plan_id:
                if day_name not in c.get("off_days", []) and target_str not in c.get("holidays", []):
                    matched_sample_count += 1
 
        pending_enq_count = 0
        if not tf:   # only show enquiry counts to admin
            pending_enq_count = enquiries_col.count_documents({
                "tag":    "form_enquiry",
                "status": "pending",
                "plan":   {"$regex": re.escape(plan_name[:20]), "$options": "i"}
            }) if plan_name else 0
 
        if active_count == 0 and matched_sample_count == 0 and pending_enq_count == 0:
            continue
 
        result.append({
            "plan_id":           plan_id,
            "plan_name":         plan_name,
            "plan_price":        plan.get("price_per_month") or plan.get("price_per_day") or 0,
            "duration_days":     plan.get("duration_days", 0),
            "active_customers":  active_count,
            "sample_boxes":      matched_sample_count,
            "pending_enquiries": pending_enq_count,
            "total_boxes":       active_count + matched_sample_count,
            "teams":             sorted(team_breakdown.values(), key=lambda x: -x["count"]),
            "is_sample_plan":    False,
            # ── alternate plan info ──
            "is_alternate_plan": bool(alt_items and len(alt_items) >= 2),
            "alternate_items":   alt_items,
            "today_item":        alt_label,        # e.g. "Sprouts"
            "is_next_day":       is_next_day,
            "target_date":       target_str,
        })
 
    # Unmatched sample customers
    unmatched_samples: dict = {}
    for c in sample_customers:
        if c.get("plan_id"):
            continue
        if day_name in c.get("off_days", []):
            continue
        if target_str in c.get("holidays", []):
            continue
        label = c.get("sample", "Sample")
        unmatched_samples[label] = unmatched_samples.get(label, 0) + 1
 
    for label, count in unmatched_samples.items():
        result.append({
            "plan_id":           None,
            "plan_name":         label,
            "plan_price":        None,
            "duration_days":     None,
            "active_customers":  0,
            "sample_boxes":      count,
            "pending_enquiries": 0,
            "total_boxes":       count,
            "teams":             [],
            "is_sample_plan":    True,
            "is_alternate_plan": False,
            "alternate_items":   [],
            "today_item":        None,
            "is_next_day":       is_next_day,
            "target_date":       target_str,
        })
 
    return jsonify(result)

# ---------------------------------------------------------------------------
# ADMIN – Sample customers detail for Boxes view
# ---------------------------------------------------------------------------
@app.route("/api/admin/sample_customers")
@login_required
@role_required("admin", "manager")
def api_sample_customers():
    label     = request.args.get("label", "").strip()
    today_str = datetime.now().strftime("%Y-%m-%d")
    day_name  = datetime.now().strftime("%A")
    query = {
        "role":   "customer",
        "status": "active",
        "sample": {"$exists": True, "$nin": [None, ""]},
    }
    if label:
        query["sample"] = label
    customers = list(users_col.find(query))
    teams     = list(teams_col.find())
    team_map  = {str(t["_id"]): t for t in teams}
    result = []
    for c in customers:
        if day_name in c.get("off_days", []):
            continue
        if today_str in c.get("holidays", []):
            continue
        team = team_map.get(str(c.get("team_id", "")), {})
        start_date = c.get("start_date")
        if isinstance(start_date, datetime):
            start_date = start_date.strftime("%Y-%m-%d")
        result.append({
            "id":             str(c["_id"]),
            "name":           c.get("name", ""),
            "phone":          c.get("phone", "—"),
            "address":        c.get("address", "—"),
            "preferred_time": c.get("preferred_time", "—"),
            "start_date":     start_date or "—",
            "sample_label":   c.get("sample", ""),
            "team_id":        str(c.get("team_id", "")),
            "team_name":      team.get("name", "—"),
            "team_area":      team.get("area", ""),
            "team_city":      team.get("city", ""),
            "off_days":       c.get("off_days", []),
            "status":         c.get("status", "active"),
        })
    all_teams = [{"id": str(t["_id"]), "name": t.get("name",""), "area": t.get("area",""), "city": t.get("city","")} for t in teams]
    return jsonify({"customers": result, "teams": all_teams})


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
@role_required("admin")          # admin only
def api_managers():
    managers = list(users_col.find({"role": "manager"}))
    teams    = list(teams_col.find())
    team_map = {str(t["_id"]): t for t in teams}
    result = []
    for m in managers:
        team = team_map.get(str(m.get("team_id", "")), {})
        result.append({
            "id":        str(m["_id"]),
            "name":      m.get("name", ""),
            "email":     m.get("email", ""),
            "phone":     m.get("phone", "—"),
            "team_id":   str(m.get("team_id", "")),
            "team_name": team.get("name", "—"),
            "team_city": team.get("city", ""),
            "team_area": team.get("area", ""),
        })
    return jsonify(result)
 
 
@app.route("/admin/managers/assign-team", methods=["POST"])
@login_required
@role_required("admin")
def assign_team_to_manager():
    data       = request.get_json(silent=True) or {}
    manager_id = data.get("manager_id")
    team_id    = data.get("team_id")
    if not manager_id:
        return jsonify({"success": False, "message": "manager_id required"}), 400
    update = {}
    if team_id:
        update["team_id"] = ObjectId(team_id)
    else:
        update["team_id"] = None
    users_col.update_one({"_id": ObjectId(manager_id), "role": "manager"}, {"$set": update})
    team = teams_col.find_one({"_id": ObjectId(team_id)}) if team_id else None
    return jsonify({"success": True, "team_name": team.get("name", "") if team else "None"})


# ---------------------------------------------------------------------------
# N8N – Add enquiry from WhatsApp incoming message
# ---------------------------------------------------------------------------
@app.route("/api/enq/add_enquiry", methods=["POST"])
def n8n_add_enquiry():
    token = request.headers.get("X-Api-Key") or request.args.get("token")
    if token != os.getenv("Token_SECRET"):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    push_name    = (data.get("pushName") or "").strip()
    whatsapp_id  = (data.get("from") or "").strip()
    body         = (data.get("body") or "").strip()
    

    if not whatsapp_id:
        return jsonify({"success": False, "message": "Missing 'from' (whatsappId)."}), 400

    # Deduplicate – don't create if this whatsapp_id already has a pending enquiry
    existing = enquiries_col.find_one({
        "whatsapp_id": whatsapp_id,
        "tag":         "form_enquiry",
        "status":      "pending"
    })
    if existing:
        return jsonify({
            "success":    False,
            "message":    "Pending enquiry already exists for this WhatsApp ID.",
            "enquiry_id": str(existing["_id"])
        }), 409

    # Derive phone from whatsapp_id (strip @lid / @s.whatsapp.net etc.)
    raw_phone = whatsapp_id.split("@")[0]
    phone = "".join(filter(str.isdigit, raw_phone))

    enquiry_doc = {
        "name":           push_name,
        
        "whatsapp_id":    whatsapp_id,
        "source":         "whatsapp",
        "tag":            "form_enquiry",
        "created_at":     datetime.now(),
    }

    result = enquiries_col.insert_one(enquiry_doc)
    enq_id = str(result.inserted_id)

    # Seed the WA inbox thread with the opening message if body is present
    if body:
        _wa_append_message(
            enquiry_id=enq_id,
            direction="in",
            body=body,
            whatsapp_id=whatsapp_id,
            phone=phone,
            push_name=push_name,
        )

    return jsonify({
        "success":    True,
        "enquiry_id": enq_id,
        "name":       push_name or phone,
        "phone":      phone,
        "message":    "Enquiry created."
    }), 201



# ---------------------------------------------------------------------------
# EMPLOYEE – Update customer delivery location (add to app.py)
# ---------------------------------------------------------------------------
 
@app.route("/api/employee/update_location", methods=["POST"])
@login_required
@role_required("employee")
def update_customer_location():
    """
    Employee updates GPS coordinates for a customer's delivery location.
    Body JSON: { "customer_id": "...", "lat": 27.123, "lng": 78.456 }
    Stores in users_col under delivery_lat / delivery_lng + updated_at + updated_by.
    """
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
 
    # Verify customer belongs to the employee's team
    employee = users_col.find_one({"_id": ObjectId(session["user_id"])})
    customer = users_col.find_one({"_id": ObjectId(customer_id), "role": "customer"})
 
    if not customer:
        return jsonify({"success": False, "message": "Customer not found."}), 404
 
    if employee and employee.get("team_id") and str(customer.get("team_id")) != str(employee.get("team_id")):
        return jsonify({"success": False, "message": "Unauthorized: not your team's customer."}), 403
 
    users_col.update_one(
        {"_id": ObjectId(customer_id)},
        {"$set": {
            "delivery_lat":        lat,
            "delivery_lng":        lng,
            "delivery_loc_updated": datetime.now(),
            "delivery_loc_by":     session.get("name", session["user_id"]),
        }}
    )
 
    # Also log in deliveries_col for today's record if exists
    today_str = datetime.now().strftime("%Y-%m-%d")
    deliveries_col.update_one(
        {"customer_id": ObjectId(customer_id), "date": today_str},
        {"$set": {
            "delivery_lat": lat,
            "delivery_lng": lng,
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
    """Returns stored delivery_lat/lng for a customer if available."""
    customer = users_col.find_one(
        {"_id": ObjectId(customer_id)},
        {"delivery_lat": 1, "delivery_lng": 1, "delivery_loc_updated": 1, "delivery_loc_by": 1, "name": 1}
    )
    if not customer:
        return jsonify({"success": False, "message": "Not found."}), 404
 
    lat = customer.get("delivery_lat")
    lng = customer.get("delivery_lng")
    updated = customer.get("delivery_loc_updated")
 
    return jsonify({
        "success":     True,
        "has_location": lat is not None and lng is not None,
        "lat":         lat,
        "lng":         lng,
        "updated_at":  updated.isoformat() if isinstance(updated, datetime) else None,
        "updated_by":  customer.get("delivery_loc_by", ""),
        "name":        customer.get("name", ""),
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

    # ── VIRTUAL delivery (not yet created in DB) ─────────────────
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

        # Upsert the delivery record first so we get a real _id
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

        # Now write to customer doc with correct real delivery _id in log
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

    # ── REAL delivery record ──────────────────────────────────────
    try:
        delivery = deliveries_col.find_one({"_id": ObjectId(delivery_id)})
    except Exception:
        return jsonify({"success": False, "message": "Invalid delivery_id."}), 400

    if not delivery:
        return jsonify({"success": False, "message": "Delivery not found."}), 404

    # Verify this delivery belongs to the employee's team
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

    # Write to customer doc — delivery["customer_id"] is already an ObjectId
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5757)