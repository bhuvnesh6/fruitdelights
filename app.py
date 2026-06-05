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




load_dotenv()


app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key-change-in-production")


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
    """
    Generate a short, human-readable login ID from customer name.
    e.g. "Bhuvnesh Raghav" -> "bhuvneshra4f@fruitedelights.com"
    Ensures uniqueness by appending a random hex suffix.
    """
    # Take first word max 8 chars + first 2 chars of second word if present
    parts = re.sub(r'[^a-zA-Z0-9 ]', '', name).strip().split()
    base  = parts[0][:8].lower() if parts else "user"
    if len(parts) > 1:
        base += parts[1][:2].lower()
    suffix  = secrets.token_hex(2)          # 4 hex chars
    candidate = f"{base}{suffix}@fruitedelights.com"
    # Collision guard (extremely unlikely but safe)
    while users_col.find_one({"email": candidate}):
        candidate = f"{base}{secrets.token_hex(2)}@fruitedelights.com"
    return candidate


def _resolve_customer_email(phone, existing_email=""):
    """Return a valid login-ID (email) for a customer; auto-generate from phone if blank."""
    email = (existing_email or "").strip()
    if email:
        return email
    safe_phone = "".join(filter(str.isdigit, phone or "unknown"))
    generated  = f"{safe_phone}@fruitedelights.local"
    if users_col.find_one({"email": generated}):
        generated = f"{safe_phone}_{secrets.token_hex(3)}@fruitedelights.local"
    return generated


# ---------------------------------------------------------------------------
# INVOICE HELPERS – duration_days based
# ---------------------------------------------------------------------------
def _parse_start_date(customer):
    """Return customer start_date as a date object, or None."""
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
    """
    Return (period_start, period_end) as date objects based on
    customer start_date + plan duration_days.
    """
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
    off_days = customer.get("off_days", [])
    holidays = set(customer.get("holidays", []))

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

    price_total = float(plan.get("price_per_month", plan.get("price_per_day", 0)))
    tax_pct     = float(plan.get("tax_percent", 0))

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

    price_total = float(plan.get("price_per_month", plan.get("price_per_day", 0)))
    tax_pct     = float(plan.get("tax_percent", 0))

    period_start, period_end = _calculate_invoice_period(customer, plan)

    off_days = customer.get("off_days", [])
    holidays = set(customer.get("holidays", []))
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
    users_col.update_one(
        {"_id": user["_id"]},
        {"$set": {"password": generate_password_hash(new_password)}}
    )

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

    stats = {
        "total_customers":    users_col.count_documents({"role": "customer"}),
        "total_employees":    users_col.count_documents({"role": "employee"}),
        "pending_deliveries": deliveries_col.count_documents({"status": "pending",           "date": today}),
        "delivered_today":    deliveries_col.count_documents({"status": "delivered",         "date": today}),
        "on_holiday_today":   deliveries_col.count_documents({"status": "cancelled_holiday", "date": today}),
        "active_teams":       teams_col.count_documents({}),
    }

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
    employees = list(users_col.find({"role": "employee"}))
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
    customers = list(users_col.find({"role": "customer"}))
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
    result = []
    for e in enquiries:
        result.append({
            "id":             str(e["_id"]),
            "name":           e.get("name", ""),
            "phone":          e.get("phone", ""),
            "address":        e.get("address", ""),
            "plan":           e.get("plan", ""),
            "delivery_time":  e.get("delivery_time", ""),
            "start_date":     e.get("start_date", ""),
            "status":         e.get("status", "pending"),
            "payment_method": e.get("payment_method", "upi"),
            "created_at":     e["created_at"].isoformat() if e.get("created_at") else None,
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
    plans_col.insert_one({
        "name":            request.form.get("name"),
        "description":     request.form.get("description"),
        "price_per_month": float(request.form.get("price_per_month", 0)),
        "duration_days":   int(request.form.get("duration_days", 0)),
        "tax_percent":     float(request.form.get("tax_percent", 0)),
        "created_at":      datetime.now()
    })
    flash(f"✅ Plan '{request.form.get('name')}' created successfully.", "success")
    return redirect(url_for("dashboard_admin"))


@app.route("/admin/plans/edit/<plan_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def edit_plan(plan_id):
    plans_col.update_one({"_id": ObjectId(plan_id)}, {"$set": {
        "name":            request.form.get("name"),
        "description":     request.form.get("description"),
        "price_per_month": float(request.form.get("price_per_month", 0)),
        "duration_days":   int(request.form.get("duration_days", 0)),
        "tax_percent":     float(request.form.get("tax_percent", 0)),
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
    teams = list(teams_col.find(query))
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
        plan_name = request.form.get("plan_name")
        plan      = plans_col.find_one({"name": plan_name})

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
        plan_name = request.form.get("plan_name")
        plan      = plans_col.find_one({"name": plan_name})

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

    now = datetime.now()

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

    member_since = customer.get("created_at")
    member_since_str = member_since.strftime("%d %b %Y") if isinstance(member_since, datetime) else "—"

    # Flag whether this is a sample customer (has non-empty sample field)
    is_sample    = bool(customer.get("sample"))
    sample_label = customer.get("sample", "")

    return render_template(
        "customer_detail.html",
        customer=customer,
        plan=plan,
        team=team,
        plans=plans,
        teams=teams,
        current_bill=current_bill,
        estimated_bill=estimated_bill,
        next_billing=next_billing,
        billing_history=billing_history,
        recent_deliveries=recent_deliveries,
        month_stats=month_stats,
        invoices=invoices,
        start_date_str=start_date_str,
        member_since_str=member_since_str,
        is_sample=is_sample,
        sample_label=sample_label,
        now=now,
        active_page="customers"
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
    name      = enq.get("name", "customer")
    phone     = enq.get("phone", "unknown")
    enq_plan  = (enq.get("plan") or "").strip()   # raw plan text from enquiry form

    # ── 1. Try to match enquiry plan text against plans collection ──
    #       Strategy: exact → prefix (first 20 chars) → any-word substring
    matched_plan = None
    if enq_plan:
        matched_plan = plans_col.find_one({"name": enq_plan})
        if not matched_plan:
            matched_plan = plans_col.find_one({
                "name": {"$regex": re.escape(enq_plan[:20]), "$options": "i"}
            })
        if not matched_plan:
            # Try matching first meaningful word (e.g. "Weekday" from "Weekday Sample – ₹69")
            first_word = re.split(r'[\s\-–—]', enq_plan)[0].strip()
            if len(first_word) >= 3:
                matched_plan = plans_col.find_one({
                    "name": {"$regex": re.escape(first_word), "$options": "i"}
                })

    # ── 2. Build sample field ──
    #   • Plan matched  → plan_id set, sample = None  (will behave as normal plan in future)
    #   • No match      → plan_id = None, sample = raw enquiry plan text (e.g. "Weekday Sample – ₹69")
    if matched_plan:
        plan_id_val  = matched_plan["_id"]
        sample_val   = None          # not a sample — real plan found
        plan_label   = matched_plan["name"]
    else:
        plan_id_val  = None
        sample_val   = enq_plan or "Sample"   # store raw text so boxes view can label it
        plan_label   = enq_plan or "Sample"

    # ── 3. Generate short readable login ID from customer name ──
    email = _generate_customer_login_id(name)

    # ── 4. Parse start date ──
    start_date = None
    if enq.get("start_date"):
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                start_date = datetime.strptime(enq["start_date"], fmt)
                break
            except Exception:
                continue

    # ── 5. Build user document ──
    #   role = "customer", source = "web_enquiry"
    #   sample field: present (with label) = sample customer; absent/None = normal customer
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
    # Only set the sample field when no real plan was matched
    if sample_val:
        user_data["sample"] = sample_val

    users_col.insert_one(user_data)

    # ── 6. Delete enquiry ──
    enquiries_col.delete_one({"_id": ObjectId(enquiry_id)})

    # ── 7. Send login credentials via WhatsApp ──
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

    customers = list(users_col.find({
        "role":    "customer",
        "team_id": employee["team_id"],
        "status":  {"$ne": "inactive"}
    }))

    final_deliveries = []
    for cust in customers:
        cust_id_str = str(cust["_id"])
        if day_name in cust.get("off_days", []):
            continue
        if today_str in cust.get("holidays", []):
            continue

        if cust_id_str in delivery_map:
            delivery = delivery_map[cust_id_str]
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
    return render_template("dashboard_employee.html", deliveries=final_deliveries)


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
    customer      = users_col.find_one({"_id": ObjectId(session["user_id"])})
    today_str     = datetime.now().strftime("%Y-%m-%d")
    today_delivery = deliveries_col.find_one({"customer_id": customer["_id"], "date": today_str})

    partner = None
    if today_delivery and today_delivery.get("assigned_to"):
        partner = users_col.find_one({"_id": today_delivery["assigned_to"]})

    now            = datetime.now()
    current_bill   = generate_monthly_bill(customer, now.month, now.year)
    estimated_bill = estimate_upcoming_bill(customer, now.month, now.year)

    # Sample customers (have a sample field) show a sample plan banner
    is_sample    = bool(customer.get("sample"))
    sample_label = customer.get("sample", "")

    return render_template(
        "dashboard_customer.html",
        delivery=today_delivery,
        partner=partner,
        current_bill=current_bill,
        estimated_bill=estimated_bill,
        is_sample=is_sample,
        sample_label=sample_label,
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

    today      = date.today()
    customers  = users_col.find({"role": "customer", "status": "active"})
    generated  = 0
    skipped    = 0
    failed     = 0

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

            invoice = {
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
    today_str = datetime.now().strftime("%Y-%m-%d")
    day_name  = datetime.now().strftime("%A")

    plans  = list(plans_col.find())
    teams  = list(teams_col.find())

    # ── Active customers: role=customer, status=active, NO sample field ──
    active_customers = list(users_col.find({
    "role":    "customer",
    "status":  "active",
    "plan_id": {"$ne": None},
    "$or": [
        {"sample": {"$exists": False}},
        {"sample": None},
        {"sample": ""},
    ]
}))

    # ── Sample customers: have a `sample` field set (non-empty string) ──
    sample_customers = list(users_col.find({
        "role":   "customer",
        "status": "active",
        "sample": {"$exists": True, "$nin": [None, ""]},
    }))

    team_map = {str(t["_id"]): t for t in teams}
    plan_map = {str(p["_id"]): p for p in plans}

    # ── Build per-plan rows for active customers ──
    result = []
    for plan in plans:
        plan_id   = str(plan["_id"])
        plan_name = plan.get("name", "")

        team_breakdown = {}
        active_count   = 0

        for c in active_customers:
            if str(c.get("plan_id", "")) != plan_id:
                continue
            if day_name in c.get("off_days", []):
                continue
            if today_str in c.get("holidays", []):
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

        # Sample customers whose plan_id matched this plan
        matched_sample_count = 0
        for c in sample_customers:
            if str(c.get("plan_id", "")) == plan_id:
                if day_name not in c.get("off_days", []) and today_str not in c.get("holidays", []):
                    matched_sample_count += 1

        # Pending enquiries still in enquiries collection
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
        })

    # ── Build separate rows for UNmatched sample customers (plan_id = None) ──
    # Group them by their sample field text (e.g. "Weekday Sample – ₹69")
    unmatched_samples: dict = {}   # sample_label -> count
    for c in sample_customers:
        if c.get("plan_id"):   # already counted above under matched plan
            continue
        if day_name in c.get("off_days", []):
            continue
        if today_str in c.get("holidays", []):
            continue
        label = c.get("sample", "Sample")
        unmatched_samples[label] = unmatched_samples.get(label, 0) + 1

    for label, count in unmatched_samples.items():
        result.append({
            "plan_id":           None,
            "plan_name":         label,          # display the raw sample text as plan name
            "plan_price":        None,
            "duration_days":     None,
            "active_customers":  0,
            "sample_boxes":      count,
            "pending_enquiries": 0,
            "total_boxes":       count,
            "teams":             [],
            "is_sample_plan":    True,           # flag so UI can style differently
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
    

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5757)