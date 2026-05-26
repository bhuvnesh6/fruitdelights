from flask import Flask, render_template, request, redirect, session, url_for, flash, jsonify
from pymongo import MongoClient
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import os
from bson.objectid import ObjectId
import secrets
from datetime import datetime, timedelta
import calendar
from functools import wraps
from mail import send_invoice_email


load_dotenv()
print("CRON_SECRET:", os.getenv("CRON_SECRET"))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key-change-in-production")

# Mongo Config
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")
client = MongoClient(MONGO_URI)
db = client[DB_NAME]

# Collections
users_col     = db.users
teams_col     = db.teams
plans_col     = db.plans
deliveries_col = db.deliveries
invoices_col = db.invoices

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
def send_credentials_email(email, password, role):
    print(f"EMAIL SENT TO: {email} | Role: {role} | Pass: {password}")


def generate_monthly_bill(customer, month, year):
    """Calculates pro-rated bill excluding holidays and off-days."""
    plan = plans_col.find_one({"_id": ObjectId(customer["plan_id"])}) if customer.get("plan_id") else None
    if not plan:
        return 0

    price_per_day = float(plan.get("price_per_day", 0))
    tax_pct       = float(plan.get("tax_percent", 0))

    joined_date = customer.get("created_at", datetime.min)
    start_day   = 1
    if joined_date.year == year and joined_date.month == month:
        start_day = joined_date.day

    _, num_days = calendar.monthrange(year, month)

    billable_days = 0
    for day in range(start_day, num_days + 1):
        current_date = datetime(year, month, day)
        date_str     = current_date.strftime("%Y-%m-%d")
        day_name     = current_date.strftime("%A")

        if day_name in customer.get("off_days", []):
            continue
        if date_str in customer.get("holidays", []):
            continue

        delivery = deliveries_col.find_one({
            "customer_id": ObjectId(customer["_id"]),
            "date": date_str,
            "status": "delivered"
        })
        if delivery:
            billable_days += 1

    subtotal = billable_days * price_per_day
    tax      = subtotal * (tax_pct / 100)
    return round(subtotal + tax, 2)


def estimate_upcoming_bill(customer, month, year):
    """Estimate bill for current/future month based on plan + known holidays."""
    plan = plans_col.find_one({"_id": ObjectId(customer["plan_id"])}) if customer.get("plan_id") else None
    if not plan:
        return {"days": 0, "subtotal": 0, "tax": 0, "total": 0}

    price_per_day = float(plan.get("price_per_day", 0))
    tax_pct       = float(plan.get("tax_percent", 0))

    joined_date = customer.get("created_at", datetime.min)
    start_day   = 1
    if joined_date.year == year and joined_date.month == month:
        start_day = joined_date.day

    _, num_days     = calendar.monthrange(year, month)
    billable_days   = 0

    for day in range(start_day, num_days + 1):
        current_date = datetime(year, month, day)
        date_str     = current_date.strftime("%Y-%m-%d")
        day_name     = current_date.strftime("%A")

        if day_name in customer.get("off_days", []):
            continue
        if date_str in customer.get("holidays", []):
            continue
        billable_days += 1

    subtotal = round(billable_days * price_per_day, 2)
    tax      = round(subtotal * tax_pct / 100, 2)
    return {
        "days":     billable_days,
        "subtotal": subtotal,
        "tax":      tax,
        "total":    round(subtotal + tax, 2),
        "plan":     plan
    }

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
    return redirect(url_for("login"))


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
        "pending_deliveries": deliveries_col.count_documents({"status": "pending", "date": today}),
        "delivered_today":    deliveries_col.count_documents({"status": "delivered", "date": today}),
        "on_holiday_today":   deliveries_col.count_documents({"status": "cancelled_holiday", "date": today}),
        "active_teams":       teams_col.count_documents({}),
    }
    
    teams = list(teams_col.find())
    plans = list(plans_col.find())

    # Convert ObjectId to string
    for team in teams:
     team["_id"] = str(team["_id"])

    for plan in plans:
     plan["_id"] = str(plan["_id"])
    
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
        result.append({
            "id":        str(c["_id"]),
            "name":      c.get("name", ""),
            "email":     c.get("email", ""),
            "phone":     c.get("phone", "—"),
            "team_id":   str(c.get("team_id", "")),
            "team_name": team.get("name", "—"),
            "team_city": team.get("city", ""),
            "plan_name": plan.get("name", "—"),
            "off_days":  c.get("off_days", []),
        })
    return jsonify(result)
# ---------------------------------------------------------------------------
# ADMIN – analytics API (for charts with date-range filter)
# ---------------------------------------------------------------------------
@app.route("/api/admin/analytics")
@login_required
@role_required("admin", "manager")
def admin_analytics():
    """Returns delivery stats for charting. ?range=7|30|today"""
    range_days = request.args.get("range", "7")
    try:
        days = int(range_days)
    except ValueError:
        days = 7

    results = []
    for i in range(days - 1, -1, -1):
        d        = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        results.append({
            "date":      d,
            "delivered": deliveries_col.count_documents({"status": "delivered",        "date": d}),
            "pending":   deliveries_col.count_documents({"status": "pending",          "date": d}),
            "holiday":   deliveries_col.count_documents({"status": "cancelled_holiday","date": d}),
        })

    # Area-wise pending for today
    today = datetime.now().strftime("%Y-%m-%d")
    teams = list(teams_col.find())
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
        "name":          request.form.get("name"),
        "description":   request.form.get("description"),
        "price_per_day": float(request.form.get("price_per_day", 0)),
        "tax_percent":   float(request.form.get("tax_percent", 0)),
        "created_at":    datetime.now()
    })
    plan_name = request.form.get("name")

    flash(f"✅ Plan '{plan_name}' created successfully.", "success")
    return redirect(url_for("dashboard_admin"))


@app.route("/admin/plans/edit/<plan_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def edit_plan(plan_id):
    plans_col.update_one({"_id": ObjectId(plan_id)}, {"$set": {
        "name":          request.form.get("name"),
        "description":   request.form.get("description"),
        "price_per_day": float(request.form.get("price_per_day", 0)),
        "tax_percent":   float(request.form.get("tax_percent", 0)),
    }})
    flash("Plan updated.", "success")
    return redirect(url_for("list_plans"))


@app.route("/admin/plans/delete/<plan_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def delete_plan(plan_id):
    plans_col.delete_one({"_id": ObjectId(plan_id)})
    flash("Plan deleted.", "warning")
    return redirect(url_for("list_plans"))

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

    # Enrich with employee count
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
    team_name = request.form.get("name")

    flash(f"✅ Team '{team_name}' added successfully.", "success")
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

    # Filter by city via team
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
    role         = request.form.get("role")
    email        = request.form.get("email")
    raw_password = secrets.token_urlsafe(8)

    user_data = {
        "name":       request.form.get("name"),
        "email":      email,
        "phone":      request.form.get("phone"),
        "role":       role,
        "password":   generate_password_hash(raw_password),
        "created_at": datetime.now()
    }

    if role in ["employee", "customer"]:
        user_data["team_id"] = ObjectId(request.form.get("team_id"))

    if role == "customer":
        plan_name = request.form.get("plan_name")
        plan      = plans_col.find_one({"name": plan_name})
        user_data.update({
            "plan_id":        plan["_id"] if plan else None,
            "address":        request.form.get("address"),
            "preferred_time": request.form.get("preferred_time"),
            "off_days":       request.form.getlist("off_days"),
            "holidays":       []
        })

    users_col.insert_one(user_data)
    send_credentials_email(email, raw_password, role)
    user_name = request.form.get("name")

    flash(f"✅ {role.capitalize()} '{user_name}' added successfully.", "success")

    return redirect(url_for("dashboard_admin"))


@app.route("/admin/users/edit/<user_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def edit_user(user_id):
    update = {
        "name":  request.form.get("name"),
        "email": request.form.get("email"),
        "phone": request.form.get("phone"),
    }
    role = request.form.get("role")
    if role == "customer":
        plan_name = request.form.get("plan_name")
        plan      = plans_col.find_one({"name": plan_name})
        update.update({
            "plan_id":        plan["_id"] if plan else None,
            "address":        request.form.get("address"),
            "preferred_time": request.form.get("preferred_time"),
            "off_days":       request.form.getlist("off_days"),
        })
        if request.form.get("team_id"):
            update["team_id"] = ObjectId(request.form.get("team_id"))

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
# ADMIN – customers
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
    customer = users_col.find_one({"_id": ObjectId(customer_id)})
    if not customer:
        flash("Customer not found.", "danger")
        return redirect(url_for("list_customers"))

    now        = datetime.now()
    # Next billing date = 1st of next month
    if now.month == 12:
        next_billing = datetime(now.year + 1, 1, 1)
    else:
        next_billing = datetime(now.year, now.month + 1, 1)

    current_bill   = generate_monthly_bill(customer, now.month, now.year)
    estimated_bill = estimate_upcoming_bill(customer, now.month, now.year)

    # Last 3 months history
    billing_history = []
    for delta in range(1, 4):
        m = now.month - delta
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        billing_history.append({
            "label": datetime(y, m, 1).strftime("%B %Y"),
            "amount": generate_monthly_bill(customer, m, y)
        })

    plan     = plans_col.find_one({"_id": ObjectId(customer["plan_id"])}) if customer.get("plan_id") else None
    team     = teams_col.find_one({"_id": customer.get("team_id")})
    plans    = list(plans_col.find())
    teams    = list(teams_col.find())

    return render_template("customer_detail.html",
                           customer=customer,
                           plan=plan,
                           team=team,
                           plans=plans,
                           teams=teams,
                           current_bill=current_bill,
                           estimated_bill=estimated_bill,
                           next_billing=next_billing,
                           billing_history=billing_history,
                           active_page="customers")

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

    today_str = datetime.now().strftime("%Y-%m-%d")
    day_name = datetime.now().strftime("%A")
    emp_id_str = str(employee["_id"])

    # 1. Fetch all real delivery entries created for this team today
    existing_deliveries = list(deliveries_col.find({
        "team_id": employee["team_id"],
        "date": today_str
    }))
    
    # Map existing entries by customer_id string for easy lookup
    delivery_map = {str(d["customer_id"]): d for d in existing_deliveries}

    # 2. Fetch all customers belonging to this same team
    customers = list(users_col.find({
        "role": "customer",
        "team_id": employee["team_id"]
    }))

    final_deliveries = []

    for cust in customers:
        cust_id_str = str(cust["_id"])
        
        # Skip if today is one of the customer's scheduled off days or holidays
        if day_name in cust.get("off_days", []):
            continue
        if today_str in cust.get("holidays", []):
            continue

        if cust_id_str in delivery_map:
            delivery = delivery_map[cust_id_str]
            assigned_to = delivery.get("assigned_to")
            assigned_to_str = str(assigned_to) if assigned_to else None
            
            # Skip if it was cancelled
            if delivery.get("status") == "cancelled_holiday":
                continue
                
            # Foolproof string comparison for assigned tasks
            # If it's assigned, but NOT to this logged-in employee, skip it!
            if assigned_to_str and assigned_to_str != emp_id_str:
                continue
                
            # Attach the customer record directly for the template lookup loop
            delivery["customer"] = [cust]
            final_deliveries.append(delivery)
        else:
            # VIRTUAL DOCUMENT: No document in delivery collection yet!
            virtual_delivery = {
                "_id": f"virtual_{cust_id_str}", 
                "customer_id": cust["_id"],
                "team_id": employee["team_id"],
                "date": today_str,
                "status": "pending",
                "assigned_to": None,
                "preferred_time": cust.get("preferred_time", "Anytime"),
                "customer": [cust] 
            }
            final_deliveries.append(virtual_delivery)

    return render_template("dashboard_employee.html", deliveries=final_deliveries)

@app.route("/employee/accept/<delivery_id>")
@login_required
@role_required("employee")
def accept_delivery(delivery_id):
    employee_id = ObjectId(session["user_id"])
    today_str = datetime.now().strftime("%Y-%m-%d")

    if delivery_id.startswith("virtual_"):
        # Extract the real customer id from the string
        customer_id_str = delivery_id.replace("virtual_", "")
        customer = users_col.find_one({"_id": ObjectId(customer_id_str)})
        
        # Double check to prevent race conditions (if another employee grabbed it first)
        exists = deliveries_col.find_one({"customer_id": customer["_id"], "date": today_str})
        if not exists:
            deliveries_col.insert_one({
                "customer_id": customer["_id"],
                "team_id": customer["team_id"],
                "date": today_str,
                "status": "accepted",
                "assigned_to": employee_id,
                "preferred_time": customer.get("preferred_time")
            })
    else:
        # It's an actual document inside MongoDB
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
    today_str = datetime.now().strftime("%Y-%m-%d")

    if delivery_id.startswith("virtual_"):
        # Directly completing an un-accepted item
        customer_id_str = delivery_id.replace("virtual_", "")
        customer = users_col.find_one({"_id": ObjectId(customer_id_str)})
        
        exists = deliveries_col.find_one({"customer_id": customer["_id"], "date": today_str})
        if not exists:
            deliveries_col.insert_one({
                "customer_id": customer["_id"],
                "team_id": customer["team_id"],
                "date": today_str,
                "status": "delivered",
                "assigned_to": employee_id,
                "preferred_time": customer.get("preferred_time"),
                "delivered_at": datetime.now()
            })
    else:
        # Standard update structure
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
    customer  = users_col.find_one({"_id": ObjectId(session["user_id"])})
    today_str = datetime.now().strftime("%Y-%m-%d")

    today_delivery = deliveries_col.find_one({
        "customer_id": customer["_id"],
        "date": today_str
    })

    partner = None
    if today_delivery and today_delivery.get("assigned_to"):
        partner = users_col.find_one({
            "_id": today_delivery["assigned_to"]
        })

    now = datetime.now()

    current_bill = generate_monthly_bill(
        customer,
        now.month,
        now.year
    )

    estimated_bill = estimate_upcoming_bill(
        customer,
        now.month,
        now.year
    )

    return render_template(
        "dashboard_customer.html",
        delivery=today_delivery,
        partner=partner,
        current_bill=current_bill,
        estimated_bill=estimated_bill,
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
# CRON
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
                "customer_id":     cust["_id"],
                "team_id":         cust["team_id"],
                "date":            date_str,
                "status":          "pending",
                "assigned_to":     None,
                "preferred_time":  cust.get("preferred_time")
            })
            count += 1

    return jsonify({"status": "success", "generated_deliveries": count})


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

    customers = users_col.find(
        {"role": "customer"},
        no_cursor_timeout=True
    ).batch_size(100)

    generated = 0
    failed = 0

    for customer in customers:

        try:

            existing = invoices_col.find_one({
                "customer_id": customer["_id"],
                "month": month,
                "year": year
            })

            if existing:
                continue

            estimate = estimate_upcoming_bill(
                customer,
                month,
                year
            )

            total = generate_monthly_bill(
                customer,
                month,
                year
            )

            invoice = {
                "customer_id": customer["_id"],
                "month": month,
                "year": year,

                "billable_days": estimate["days"],

                "subtotal": estimate["subtotal"],
                "tax": estimate["tax"],
                "total": total,

                "status": "sent",

                "generated_at": datetime.now()
            }

            invoices_col.insert_one(invoice)

            send_invoice_email(customer, invoice)

            generated += 1

        except Exception as e:

            failed += 1

            print("INVOICE ERROR:", str(e))

    return jsonify({
        "success": True,
        "generated": generated,
        "failed": failed
    })  
  
  
  
    
@app.route("/test-invoice", methods=["GET", "POST"])
def test_invoice():

    if request.method == "POST":

        name = request.form.get("name")
        email = request.form.get("email")

        customer = {
            "name": name,
            "email": email
        }

        invoice = {
            "month": 5,
            "year": 2026,

            "billable_days": 24,

            "subtotal": 2400,
            "tax": 120,
            "total": 2520
        }

        send_invoice_email(customer, invoice)

        flash("Mock invoice email sent successfully!", "success")

        return redirect(url_for("test_invoice"))

    return render_template("test_invoice.html")   
    
    
    
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5757)