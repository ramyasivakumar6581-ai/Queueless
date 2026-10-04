import os, re, csv, io, hmac, sqlite3
from datetime import date, datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, g, abort, Response)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database", "database.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

app = Flask(__name__,
            template_folder=os.path.join(BASE_DIR, "..", "frontend", "templates"),
            static_folder=os.path.join(BASE_DIR, "..", "frontend", "static"))
app.secret_key = os.environ.get("SECRET_KEY", "queueless_secret")
# Secret key required to register a new admin (override with an environment variable)
ADMIN_REGISTER_KEY = os.environ.get("ADMIN_REGISTER_KEY", "QUEUELESS-ADMIN-2026")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,30}$")
MIN_PASSWORD_LEN = 6
STATUSES = ("ACTIVE", "FINISHED", "CANCELED")
STATUS_LABEL = {"ACTIVE": "Waiting", "FINISHED": "Served", "CANCELED": "Canceled"}

# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS admin (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    state TEXT NOT NULL, district TEXT NOT NULL, bank TEXT NOT NULL, branch TEXT NOT NULL,
    place TEXT NOT NULL, service TEXT NOT NULL, token_date TEXT NOT NULL, token_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS banks (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, code TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS branches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, bank_id INTEGER NOT NULL, name TEXT NOT NULL, location TEXT NOT NULL,
    UNIQUE (bank_id, name), FOREIGN KEY (bank_id) REFERENCES banks(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS services (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, duration INTEGER NOT NULL DEFAULT 5);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, token_id INTEGER,
    title TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_tokens_user  ON tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_tokens_queue ON tokens(bank, branch, token_date, status);
CREATE INDEX IF NOT EXISTS idx_notif_user   ON notifications(user_id);
"""

def init_schema(reset=False):
    """Create tables, normalise old data and seed defaults. Safe to run many times."""
    conn = sqlite3.connect(DB_PATH)
    if reset:
        for t in ("notifications", "tokens", "branches", "banks", "services", "settings", "users", "admin"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    # old rows used mixed-case / different words for status
    for new, olds in (("ACTIVE", ("booked", "active")), ("CANCELED", ("canceled", "cancelled")),
                      ("FINISHED", ("finished", "served", "completed"))):
        conn.execute(f"UPDATE tokens SET status=? WHERE LOWER(status) IN ({','.join('?'*len(olds))}) AND status<>?",
                     (new, *olds, new))
    conn.execute("UPDATE tokens SET bank='State Bank of India' WHERE bank IN ('SBI', 'sbi')")
    if not conn.execute("SELECT 1 FROM admin LIMIT 1").fetchone():
        conn.execute("INSERT INTO admin (username, password) VALUES (?, ?)", ("admin", generate_password_hash("admin123")))
    if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        conn.execute("INSERT INTO users (username, password) VALUES (?, ?)", ("user", generate_password_hash("user123")))
    if not conn.execute("SELECT 1 FROM banks LIMIT 1").fetchone():
        seed = {"State Bank of India": ("SBIN", [("Main Branch", "Chennai"), ("Katpadi Branch", "Vellore"), ("Kudithala Branch", "Madurai")]),
                "HDFC Bank": ("HDFC", [("Main Branch", "Chennai"), ("RS Puram Branch", "Coimbatore")]),
                "ICICI Bank": ("ICIC", [("Main Branch", "Chennai"), ("Anna Nagar Branch", "Chennai")])}
        for bank, (code, branches) in seed.items():
            bid = conn.execute("INSERT INTO banks (name, code) VALUES (?, ?)", (bank, code)).lastrowid
            conn.executemany("INSERT INTO branches (bank_id, name, location) VALUES (?, ?, ?)",
                             [(bid, n, loc) for n, loc in branches])
    if not conn.execute("SELECT 1 FROM services LIMIT 1").fetchone():
        conn.executemany("INSERT INTO services (name, duration) VALUES (?, ?)",
                         [("Deposit", 5), ("Withdraw", 5), ("Account Opening", 15), ("Passbook Update", 3), ("Loan Enquiry", 10)])
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('max_queue', '50')")
    conn.commit()
    conn.close()

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db:
        db.close()

def q(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    return cur.fetchone() if one else cur.fetchall()

def run(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur

def get_setting(key, default):
    row = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return row["value"] if row else default

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def notify(user_id, token_id, title, message):
    run("INSERT INTO notifications (user_id, token_id, title, message, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, token_id, title, message, now()))

def token_counts(where="", args=()):
    counts = {s: 0 for s in STATUSES}
    for r in q(f"SELECT status, COUNT(*) c FROM tokens {where} GROUP BY status", args):
        counts[r["status"]] = r["c"]
    counts["total"] = sum(counts[s] for s in STATUSES)
    return counts

def login_required(role):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **k):
            if session.get("role") != role:
                return redirect(url_for(f"{role}_login"))
            return f(*a, **k)
        return wrapper
    return deco

@app.context_processor
def inject_globals():
    return {"STATUS_LABEL": STATUS_LABEL, "today": date.today().isoformat()}

# ---------------------------------------------------------------- home / auth
@app.route("/")
def home():
    return render_template("home.html")

@app.route("/register")
def register():
    return render_template("register.html")

def validate_credentials(username, password, confirm):
    if not username or not password or not confirm:
        return "All fields are required."
    if not USERNAME_RE.match(username):
        return "Username must be 3-30 characters (letters, numbers and _ only)."
    if len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if password != confirm:
        return "Password and Confirm Password do not match."
    return None

def create_account(table, username, password):
    if table not in ("users", "admin"):
        raise ValueError("invalid table")
    if q(f"SELECT 1 FROM {table} WHERE LOWER(username)=LOWER(?)", (username,), one=True):
        return False
    try:
        run(f"INSERT INTO {table} (username, password) VALUES (?, ?)", (username, generate_password_hash(password)))
    except sqlite3.IntegrityError:
        return False
    return True

def _login(role, table):
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        row = q(f"SELECT * FROM {table} WHERE LOWER(username)=LOWER(?)", (username,), one=True)
        if row and check_password_hash(row["password"], request.form.get("password", "")):
            session.clear()
            session.update(user_id=row["id"], role=role, username=row["username"])
            return redirect(url_for(f"{role}_dashboard"))
        flash(f"Invalid {role} credentials.", "error")
    return render_template("auth.html", role=role, mode="login", username="")

def _register(role, table):
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        err = validate_credentials(username, password, request.form.get("confirm_password", ""))
        if not err and role == "admin" and not hmac.compare_digest(
                request.form.get("admin_key", "").encode(), ADMIN_REGISTER_KEY.encode()):
            err = "Invalid admin secret key."
        if not err and not create_account(table, username, password):
            err = "That username is already taken."
        if err:
            flash(err, "error")
            return render_template("auth.html", role=role, mode="register", username=username)
        flash("Account created successfully. Please log in.", "success")
        return redirect(url_for(f"{role}_login"))
    return render_template("auth.html", role=role, mode="register", username="")

@app.route("/user-login", methods=["GET", "POST"])
def user_login():
    return _login("user", "users")

@app.route("/admin-login", methods=["GET", "POST"])
def admin_login():
    return _login("admin", "admin")

@app.route("/user-register", methods=["GET", "POST"])
def user_register():
    return _register("user", "users")

@app.route("/admin-register", methods=["GET", "POST"])
def admin_register():
    return _register("admin", "admin")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

# ---------------------------------------------------------------- dashboards
@app.route("/user-dashboard")
@login_required("user")
def user_dashboard():
    tiles = [("📝", "Book Token", "Reserve your slot at a branch", "book_token", {}),
             ("📄", "My Tokens", "Track your position in the queue", "view_token", {}),
             ("❌", "Cancel Token", "Cancel a waiting token", "view_token", {"status": "ACTIVE"}),
             ("🔔", "Notifications", "Updates about your tokens", "notifications", {})]
    return render_template("dashboard.html", heading="Your tokens", tiles=tiles,
                           stats=token_counts("WHERE user_id=?", (session["user_id"],)))

@app.route("/admin-dashboard")
@login_required("admin")
def admin_dashboard():
    tiles = [("🗂️", "Reports", "Filter, serve and export tokens", "reports", {}),
             ("📊", "Analytics", "Bank, branch and service insights", "analytics", {}),
             ("🔔", "Notifications", "Send updates to customers", "notifications", {}),
             ("⚙️", "Settings", "Banks, branches, services and admins", "settings", {})]
    return render_template("dashboard.html", heading="Today's tokens", tiles=tiles,
                           stats=token_counts("WHERE token_date=?", (date.today().isoformat(),)))

# ---------------------------------------------------------------- user: tokens
@app.route("/book-token", methods=["GET", "POST"])
@login_required("user")
def book_token():
    if request.method == "POST":
        f = {k: request.form.get(k, "").strip() for k in
             ("state", "district", "bank", "branch", "service", "token_date", "token_time")}
        error = None
        if not all(f.values()):
            error = "All fields are required."
        else:
            try:
                if datetime.strptime(f["token_date"], "%Y-%m-%d").date() < date.today():
                    error = "Please choose today or a future date."
                datetime.strptime(f["token_time"], "%H:%M")
            except ValueError:
                error = "Invalid date or time."
        branch = None if error else q(
            "SELECT r.location FROM branches r JOIN banks b ON b.id=r.bank_id WHERE b.name=? AND r.name=?",
            (f["bank"], f["branch"]), one=True)
        if not error and not branch:
            error = "Please select a valid bank and branch."
        if not error and not q("SELECT 1 FROM services WHERE name=?", (f["service"],), one=True):
            error = "Please select a valid service."
        if not error:
            where = "bank=? AND branch=? AND token_date=? AND status='ACTIVE'"
            args = (f["bank"], f["branch"], f["token_date"])
            if q(f"SELECT 1 FROM tokens WHERE user_id=? AND {where}", (session["user_id"], *args), one=True):
                error = "You already have a waiting token for this branch and date."
            elif q(f"SELECT COUNT(*) c FROM tokens WHERE {where}", args, one=True)["c"] >= int(get_setting("max_queue", 50)):
                error = "The queue for this branch is full on that date. Please pick another date."
        if error:
            flash(error, "error")
            return redirect(url_for("book_token"))
        cur = run("""INSERT INTO tokens (user_id, state, district, bank, branch, place, service,
                     token_date, token_time, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,'ACTIVE',?)""",
                  (session["user_id"], f["state"], f["district"], f["bank"], f["branch"], branch["location"],
                   f["service"], f["token_date"], f["token_time"], now()))
        flash(f"Token #{cur.lastrowid} booked successfully.", "success")
        return redirect(url_for("view_token"))
    banks = q("SELECT name FROM banks ORDER BY name")
    branches = [dict(r) for r in q("SELECT b.name bank, r.name, r.location FROM branches r JOIN banks b ON b.id=r.bank_id ORDER BY r.name")]
    return render_template("book_token.html", banks=banks, branches=branches,
                           services=q("SELECT name, duration FROM services ORDER BY name"))

@app.route("/view-token")
@login_required("user")
def view_token():
    status = request.args.get("status", "").upper()
    status = status if status in STATUSES else ""
    tokens = q("""
        SELECT t.*, (SELECT COUNT(*) FROM tokens o
                     WHERE o.status='ACTIVE' AND o.bank=t.bank AND o.branch=t.branch AND o.token_date=t.token_date
                       AND (o.token_time < t.token_time OR (o.token_time = t.token_time AND o.id < t.id))) AS ahead
        FROM tokens t WHERE t.user_id=? AND (?='' OR t.status=?)
        ORDER BY t.token_date DESC, t.token_time DESC, t.id DESC""", (session["user_id"], status, status))
    avg = q("SELECT COALESCE(AVG(duration), 5) a FROM services", one=True)["a"]
    return render_template("view_token.html", tokens=tokens, status=status, avg_min=round(avg))

@app.route("/cancel-token/<int:token_id>", methods=["POST"])
@login_required("user")
def cancel_token(token_id):
    cur = run("UPDATE tokens SET status='CANCELED' WHERE id=? AND user_id=? AND status='ACTIVE'",
              (token_id, session["user_id"]))
    flash(f"Token #{token_id} canceled." if cur.rowcount else "Only your waiting tokens can be canceled.",
          "success" if cur.rowcount else "error")
    return redirect(url_for("view_token"))

@app.route("/delete-token/<int:token_id>", methods=["POST"])
@login_required("user")
def delete_token(token_id):
    cur = run("DELETE FROM tokens WHERE id=? AND user_id=? AND status<>'ACTIVE'", (token_id, session["user_id"]))
    flash("Token removed from your history." if cur.rowcount else "Cancel a waiting token before deleting it.",
          "success" if cur.rowcount else "error")
    return redirect(url_for("view_token"))

# ---------------------------------------------------------------- admin: analytics / reports
@app.route("/analytics")
@login_required("admin")
def analytics():
    cols = "COUNT(*) total, SUM(status='ACTIVE') active, SUM(status='FINISHED') finished, SUM(status='CANCELED') canceled"
    return render_template(
        "analytics.html",
        overall=token_counts(), today_counts=token_counts("WHERE token_date=?", (date.today().isoformat(),)),
        by_bank=q(f"SELECT bank, {cols} FROM tokens GROUP BY bank ORDER BY total DESC"),
        by_branch=q(f"SELECT bank, branch, {cols} FROM tokens GROUP BY bank, branch ORDER BY total DESC"),
        by_service=q("SELECT service, COUNT(*) total FROM tokens GROUP BY service ORDER BY total DESC"))

def _report_rows():
    f = {k: request.args.get(k, "").strip() for k in ("date", "branch", "status")}
    sql, args = ["SELECT t.*, u.username FROM tokens t JOIN users u ON u.id=t.user_id WHERE 1=1"], []
    for col, key in (("t.token_date", "date"), ("t.branch", "branch"), ("t.status", "status")):
        if f[key]:
            sql.append(f"AND {col}=?")
            args.append(f[key].upper() if key == "status" else f[key])
    sql.append("ORDER BY t.token_date DESC, t.token_time, t.id")
    return q(" ".join(sql), args), f

@app.route("/reports")
@login_required("admin")
def reports():
    rows, f = _report_rows()
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in STATUSES}
    return render_template("reports.html", rows=rows, f=f, counts=counts,
                           branches=[r["branch"] for r in q("SELECT DISTINCT branch FROM tokens ORDER BY branch")])

@app.route("/reports/export")
@login_required("admin")
def export_reports():
    rows, _ = _report_rows()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Token", "User", "Bank", "Branch", "Place", "Service", "Date", "Time", "Status"])
    for r in rows:
        w.writerow([r["id"], r["username"], r["bank"], r["branch"], r["place"], r["service"],
                    r["token_date"], r["token_time"], STATUS_LABEL[r["status"]]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=queueless_report.csv"})

@app.route("/admin/token/<int:token_id>/<action>", methods=["POST"])
@login_required("admin")
def token_action(token_id, action):
    new = {"serve": "FINISHED", "cancel": "CANCELED"}.get(action) or abort(404)
    t = q("SELECT * FROM tokens WHERE id=? AND status='ACTIVE'", (token_id,), one=True)
    if not t:
        flash("Only waiting tokens can be updated.", "error")
    else:
        run("UPDATE tokens SET status=? WHERE id=?", (new, token_id))
        notify(t["user_id"], token_id, f"Token #{token_id} {STATUS_LABEL[new].lower()}",
               f"Your token #{token_id} at {t['bank']} - {t['branch']} has been marked as {STATUS_LABEL[new].lower()}.")
        flash(f"Token #{token_id} marked as {STATUS_LABEL[new].lower()}.", "success")
    return redirect(request.referrer or url_for("reports"))

# ---------------------------------------------------------------- notifications
@app.route("/notifications")
def notifications():
    role = session.get("role")
    if role == "admin":
        sent = q("""SELECT n.*, u.username FROM notifications n JOIN users u ON u.id=n.user_id
                    ORDER BY n.id DESC LIMIT 20""")
        return render_template("notifications.html", sent=sent)
    if role == "user":
        return render_template("notifications.html",
                               inbox=q("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC", (session["user_id"],)))
    return redirect(url_for("user_login"))

@app.route("/send-notification", methods=["POST"])
@login_required("admin")
def send_notification():
    title, message = request.form.get("title", "").strip(), request.form.get("message", "").strip()
    token_id = request.form.get("token_id", "").strip()
    t = q("SELECT id, user_id FROM tokens WHERE id=?", (token_id,), one=True) if token_id.isdigit() else None
    if not (title and message):
        flash("Title and message are required.", "error")
    elif not t:
        flash("Token not found. Check the token number.", "error")
    else:
        notify(t["user_id"], t["id"], title, message)
        flash(f"Notification sent for token #{t['id']}.", "success")
    return redirect(url_for("notifications"))

# ---------------------------------------------------------------- admin: settings
@app.route("/settings")
@login_required("admin")
def settings():
    return render_template(
        "settings.html", tab=request.args.get("tab", "banks"),
        banks=q("SELECT * FROM banks ORDER BY name"),
        branches=q("SELECT r.*, b.name bank FROM branches r JOIN banks b ON b.id=r.bank_id ORDER BY b.name, r.name"),
        services=q("SELECT * FROM services ORDER BY name"), admins=q("SELECT id, username FROM admin ORDER BY username"),
        max_queue=get_setting("max_queue", 50))

def _settings_exec(tab, sql, args, ok):
    try:
        run(sql, args)
        flash(ok, "success")
    except sqlite3.IntegrityError:
        flash("That entry already exists or is still in use.", "error")
    return redirect(url_for("settings", tab=tab))

def _form(*keys):
    return [request.form.get(k, "").strip() for k in keys]

@app.route("/add-bank", methods=["POST"])
@login_required("admin")
def add_bank():
    name, code = _form("bank_name", "bank_code")
    if not (name and code):
        flash("Bank name and code are required.", "error")
        return redirect(url_for("settings", tab="banks"))
    return _settings_exec("banks", "INSERT INTO banks (name, code) VALUES (?, ?)", (name, code.upper()), "Bank added.")

@app.route("/add-branch", methods=["POST"])
@login_required("admin")
def add_branch():
    bank_id, name, location = _form("bank_id", "branch_name", "location")
    if not (bank_id.isdigit() and name and location):
        flash("Bank, branch name and location are required.", "error")
        return redirect(url_for("settings", tab="branches"))
    return _settings_exec("branches", "INSERT INTO branches (bank_id, name, location) VALUES (?, ?, ?)",
                          (int(bank_id), name, location), "Branch added.")

@app.route("/add-service", methods=["POST"])
@login_required("admin")
def add_service():
    name, duration = _form("service_name", "duration")
    if not (name and duration.isdigit() and int(duration) > 0):
        flash("Service name and a valid duration are required.", "error")
        return redirect(url_for("settings", tab="services"))
    return _settings_exec("services", "INSERT INTO services (name, duration) VALUES (?, ?)", (name, int(duration)), "Service added.")

@app.route("/add-admin", methods=["POST"])
@login_required("admin")
def add_admin():
    username, password = _form("username", "password")
    err = validate_credentials(username, password, password)
    if not err and not create_account("admin", username, password):
        err = "That username is already taken."
    flash(err or "Admin added.", "error" if err else "success")
    return redirect(url_for("settings", tab="admins"))

@app.route("/settings/<kind>/<int:item_id>/delete", methods=["POST"])
@login_required("admin")
def delete_item(kind, item_id):
    table = {"bank": "banks", "branch": "branches", "service": "services", "admin": "admin"}.get(kind) or abort(404)
    if kind == "admin":
        if item_id == session["user_id"]:
            flash("You cannot delete your own account.", "error")
            return redirect(url_for("settings", tab="admins"))
        if q("SELECT COUNT(*) c FROM admin", one=True)["c"] <= 1:
            flash("At least one admin must remain.", "error")
            return redirect(url_for("settings", tab="admins"))
    return _settings_exec(f"{kind}s" if kind != "admin" else "admins", f"DELETE FROM {table} WHERE id=?", (item_id,), "Deleted.")

@app.route("/update-system", methods=["POST"])
@login_required("admin")
def update_system():
    (max_queue,) = _form("max_queue")
    if not (max_queue.isdigit() and 1 <= int(max_queue) <= 1000):
        flash("Max queue size must be between 1 and 1000.", "error")
        return redirect(url_for("settings", tab="system"))
    return _settings_exec("system", "INSERT INTO settings (key, value) VALUES ('max_queue', ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (max_queue,), "Settings saved.")

init_schema()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "1") == "1")
