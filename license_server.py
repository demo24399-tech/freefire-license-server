
import os
import json
import sqlite3
import secrets
import string
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify

# =============== CONFIGURATION ===============

MASTER_SECRET = os.environ.get("MASTER_SECRET", "")
CUSTOMER_SECRET = os.environ.get("CUSTOMER_SECRET", "")

DB_PATH = os.environ.get("DB_PATH", "license_database.db")
DHAKA = ZoneInfo("Asia/Dhaka")

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# =============================================


def connect_db():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def now_dhaka():
    return datetime.now(DHAKA)


def quota_day():
    now = now_dhaka()
    if now.hour < 4:
        now -= timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def initialize_db():
    with connect_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS licenses (
            license_key TEXT PRIMARY KEY,
            duration_days INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            daily_limit INTEGER NOT NULL,
            apis TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            telegram_id INTEGER,
            customer_name TEXT DEFAULT '',
            username TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS api_settings (
            api_name TEXT PRIMARY KEY,
            api_url TEXT DEFAULT '',
            api_key TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS uid_usage (
            license_key TEXT NOT NULL,
            quota_date TEXT NOT NULL,
            uid TEXT NOT NULL,
            PRIMARY KEY (license_key, quota_date, uid)
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_key TEXT NOT NULL,
            telegram_id INTEGER NOT NULL,
            uid TEXT NOT NULL,
            region TEXT NOT NULL,
            total_likes INTEGER DEFAULT 0,
            result TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)

        for api in ("api1", "api2", "api3"):
            db.execute(
                "INSERT OR IGNORE INTO api_settings (api_name) VALUES (?)",
                (api,)
            )


initialize_db()


# =============== AUTHENTICATION ===============

def authorized(secret):
    return bool(secret) and (
        request.headers.get("X-Auth-Token", "") == secret
        or request.headers.get("X-Customer-Token", "") == secret
    )


def master_auth():
    return bool(MASTER_SECRET) and (
        request.headers.get("X-Auth-Token", "") == MASTER_SECRET
    )


def customer_auth():
    return bool(CUSTOMER_SECRET) and (
        request.headers.get("X-Customer-Token", "") == CUSTOMER_SECRET
    )


def require_master():
    if not master_auth():
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    return None


def require_customer():
    if not customer_auth():
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    return None


# =============== LICENSE HELPERS ===============

def generate_key():
    alphabet = string.ascii_uppercase + string.digits
    parts = [
        "".join(secrets.choice(alphabet) for _ in range(5))
        for _ in range(4)
    ]
    return "FF-" + "-".join(parts)


def license_expired(row):
    if not row["expires_at"]:
        return False

    expiry = datetime.fromisoformat(row["expires_at"])
    return now_dhaka() >= expiry


def get_license_by_telegram(telegram_id):
    with connect_db() as db:
        return db.execute(
            "SELECT * FROM licenses WHERE telegram_id = ?",
            (telegram_id,)
        ).fetchone()


def get_active_license(telegram_id):
    row = get_license_by_telegram(telegram_id)

    if not row:
        return None, "No active license found"

    if row["status"] != "active":
        return None, "Your license is blocked"

    if license_expired(row):
        return None, "Your license has expired"

    return row, None


def used_uids(license_key):
    with connect_db() as db:
        row = db.execute("""
            SELECT COUNT(*) AS total
            FROM uid_usage
            WHERE license_key = ? AND quota_date = ?
        """, (license_key, quota_day())).fetchone()

    return row["total"]


# =============== API RESPONSE HELPERS ===============

def to_int(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def find_value(data, names):
    if isinstance(data, dict):
        for name in names:
            if name in data:
                return data[name]

        for value in data.values():
            found = find_value(value, names)
            if found is not None:
                return found

    elif isinstance(data, list):
        for item in data:
            found = find_value(item, names)
            if found is not None:
                return found

    return None


def parse_api_response(data):
    if not isinstance(data, dict):
        return {
            "success": False,
            "likes": 0,
            "name": "Unknown",
            "before": None,
            "after": None,
            "raw": data
        }

    before = find_value(data, [
        "LikesbeforeCommand", "likes_before", "before",
        "previous_likes", "likesBefore"
    ])

    after = find_value(data, [
        "LikesafterCommand", "likes_after", "after",
        "current_likes", "likesAfter"
    ])

    given = find_value(data, [
        "LikesGivenByAPI", "likes_given", "likes_added",
        "total_likes", "likes"
    ])

    if before is not None and after is not None:
        likes = max(0, to_int(after) - to_int(before))
    else:
        likes = max(0, to_int(given))

    name = find_value(data, [
        "PlayerNickname", "nickname", "name", "player_name"
    ]) or "Unknown"

    status = find_value(data, [
        "status", "success", "message"
    ])

    success = likes > 0

    if isinstance(status, bool):
        success = success or status

    return {
        "success": success,
        "likes": likes,
        "name": str(name),
        "before": to_int(before) if before is not None else None,
        "after": to_int(after) if after is not None else None,
        "raw": data
    }


# =============== API CALLS ===============

def call_selected_api(api_name, settings, uid, region):
    url = settings["api_url"]
    key = settings["api_key"]

    if not url or not key:
        return {
            "success": False,
            "likes": 0,
            "error": "API URL or API Key is not configured"
        }

    # API 1: uid, region, key, count
    if api_name == "api1":
        params = {
            "uid": uid,
            "region": region,
            "key": key,
            "count": "max"
        }

    # API 2: uid, server_name, key
    elif api_name == "api2":
        params = {
            "uid": uid,
            "server_name": region,
            "key": key
        }

    # API 3: generic format; provider may require different parameters
    else:
        params = {
            "uid": uid,
            "region": region,
            "key": key
        }

    try:
        response = requests.get(url, params=params, timeout=45)

        try:
            data = response.json()
        except ValueError:
            data = {"message": response.text}

        if response.status_code != 200:
            return {
                "success": False,
                "likes": 0,
                "error": "HTTP " + str(response.status_code)
            }

        result = parse_api_response(data)
        result["success"] = result["likes"] > 0
        return result

    except requests.RequestException as error:
        logging.exception("%s request failed", api_name)
        return {
            "success": False,
            "likes": 0,
            "error": str(error)
        }


# =============== MASTER BOT ENDPOINTS ===============

@app.route("/admin/licenses", methods=["POST", "GET"])
def admin_licenses():
    auth_error = require_master()
    if auth_error:
        return auth_error

    if request.method == "POST":
        data = request.get_json(silent=True) or {}

        try:
            duration = int(data["duration_days"])
            daily_limit = int(data["daily_limit"])
            apis = data["apis"]

            if duration < 0 or daily_limit < 0:
                raise ValueError()

            if not isinstance(apis, list) or not apis:
                raise ValueError()

            if any(api not in ("api1", "api2", "api3") for api in apis):
                raise ValueError()

        except (KeyError, TypeError, ValueError):
            return jsonify({
                "ok": False,
                "message": "Invalid duration, limit or API selection"
            }), 400

        created = now_dhaka()

        # 0 days means Permanent
        expiry = None
        if duration > 0:
            expiry = (created + timedelta(days=duration)).isoformat()

        key = generate_key()

        with connect_db() as db:
            while db.execute(
                "SELECT 1 FROM licenses WHERE license_key = ?", (key,)
            ).fetchone():
                key = generate_key()

            db.execute("""
                INSERT INTO licenses
                (license_key, duration_days, created_at, expires_at,
                 daily_limit, apis, status)
                VALUES (?, ?, ?, ?, ?, ?, 'active')
            """, (
                key,
                duration,
                created.isoformat(),
                expiry,
                daily_limit,
                json.dumps(apis)
            ))

        return jsonify({
            "ok": True,
            "license_key": key,
            "duration_days": duration,
            "daily_limit": daily_limit,
            "apis": apis,
            "created_at": created.isoformat(),
            "expires_at": expiry
        })

    with connect_db() as db:
        rows = db.execute(
            "SELECT * FROM licenses ORDER BY created_at DESC"
        ).fetchall()

    licenses = []
    for row in rows:
        item = dict(row)
        item["apis"] = json.loads(item["apis"])
        licenses.append(item)

    return jsonify({"ok": True, "licenses": licenses})


@app.route("/admin/license-action", methods=["POST"])
def admin_license_action():
    auth_error = require_master()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    key = data.get("license_key", "").strip()
    action = data.get("action", "").lower()

    if action not in ("block", "unblock"):
        return jsonify({"ok": False, "message": "Invalid action"}), 400

    with connect_db() as db:
        cursor = db.execute(
            "UPDATE licenses SET status = ? WHERE license_key = ?",
            ("blocked" if action == "block" else "active", key)
        )

    if cursor.rowcount == 0:
        return jsonify({"ok": False, "message": "License not found"}), 404

    return jsonify({"ok": True, "message": "License updated"})


@app.route("/admin/customers", methods=["GET"])
def admin_customers():
    auth_error = require_master()
    if auth_error:
        return auth_error

    with connect_db() as db:
        rows = db.execute("""
            SELECT telegram_id, customer_name, username, license_key,
                   status, created_at, expires_at
            FROM licenses
            WHERE telegram_id IS NOT NULL
            ORDER BY created_at DESC
        """).fetchall()

    return jsonify({
        "ok": True,
        "customers": [dict(row) for row in rows]
    })


@app.route("/admin/stats", methods=["GET"])
def admin_stats():
    auth_error = require_master()
    if auth_error:
        return auth_error

    with connect_db() as db:
        licenses = db.execute(
            "SELECT COUNT(*) AS n FROM licenses"
        ).fetchone()["n"]

        active = db.execute(
            "SELECT COUNT(*) AS n FROM licenses WHERE status='active'"
        ).fetchone()["n"]

        customers = db.execute(
            "SELECT COUNT(DISTINCT telegram_id) AS n FROM licenses "
            "WHERE telegram_id IS NOT NULL"
        ).fetchone()["n"]

        orders = db.execute(
            "SELECT COUNT(*) AS n FROM orders"
        ).fetchone()["n"]

        likes = db.execute(
            "SELECT COALESCE(SUM(total_likes),0) AS n FROM orders"
        ).fetchone()["n"]

    return jsonify({
        "ok": True,
        "total_licenses": licenses,
        "active_licenses": active,
        "total_customers": customers,
        "total_orders": orders,
        "total_likes": likes
    })


@app.route("/admin/api-settings", methods=["POST"])
def admin_api_settings():
    auth_error = require_master()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}

    api = str(data.get("api", "")).lower()
    field = str(data.get("field", "")).lower()
    value = str(data.get("value", "")).strip()

    if api not in ("api1", "api2", "api3"):
        return jsonify({"ok": False, "message": "Invalid API"}), 400

    if field not in ("url", "key"):
        return jsonify({"ok": False, "message": "Invalid field"}), 400

    column = "api_url" if field == "url" else "api_key"

    with connect_db() as db:
        db.execute(
            f"UPDATE api_settings SET {column}=? WHERE api_name=?",
            (value, api)
        )

    return jsonify({"ok": True, "message": "API setting saved"})


# =============== CUSTOMER BOT ENDPOINTS ===============

@app.route("/customer/activate", methods=["POST"])
def customer_activate():
    auth_error = require_customer()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}

    key = str(data.get("license_key", "")).strip().upper()
    telegram_id = data.get("telegram_id")
    name = str(data.get("name", ""))[:100]
    username = str(data.get("username", ""))[:100]

    if not key or not telegram_id:
        return jsonify({"ok": False, "message": "Missing information"}), 400

    with connect_db() as db:
        row = db.execute(
            "SELECT * FROM licenses WHERE license_key=?", (key,)
        ).fetchone()

        if not row:
            return jsonify({"ok": False, "message": "License Key not found"})

        if row["status"] != "active":
            return jsonify({"ok": False, "message": "License is blocked"})

        if license_expired(row):
            return jsonify({"ok": False, "message": "License has expired"})

        if row["telegram_id"] and row["telegram_id"] != int(telegram_id):
            return jsonify({
                "ok": False,
                "message": "This License Key is already used by another account"
            })

        existing = db.execute(
            "SELECT license_key FROM licenses WHERE telegram_id=?",
            (telegram_id,)
        ).fetchone()

        if existing and existing["license_key"] != key:
            return jsonify({
                "ok": False,
                "message": "This Telegram account already has another license"
            })

        db.execute("""
            UPDATE licenses
            SET telegram_id=?, customer_name=?, username=?
            WHERE license_key=?
        """, (telegram_id, name, username, key))

    return jsonify({"ok": True, "message": "License activated"})


@app.route("/customer/license", methods=["GET"])
def customer_license():
    auth_error = require_customer()
    if auth_error:
        return auth_error

    telegram_id = request.args.get("telegram_id", type=int)

    if not telegram_id:
        return jsonify({"ok": False, "message": "Telegram ID required"}), 400

    row = get_license_by_telegram(telegram_id)

    if not row:
        return jsonify({"ok": True, "active": False})

    if row["status"] != "active" or license_expired(row):
        return jsonify({"ok": True, "active": False})

    apis = json.loads(row["apis"])

    return jsonify({
        "ok": True,
        "active": True,
        "license_key": "Hidden",
        "status": row["status"],
        "expires_at": row["expires_at"] or "Permanent",
        "daily_limit_text": "Unlimited" if row["daily_limit"] == 0 else row["daily_limit"],
        "used_today": used_uids(row["license_key"]),
        "apis_text": ", ".join(api.upper() for api in apis)
    })


@app.route("/customer/stats", methods=["GET"])
def customer_stats():
    auth_error = require_customer()
    if auth_error:
        return auth_error

    telegram_id = request.args.get("telegram_id", type=int)
    row, error = get_active_license(telegram_id)

    if error:
        return jsonify({"ok": False, "message": error})

    with connect_db() as db:
        stats = db.execute("""
            SELECT COUNT(*) AS orders,
                   COALESCE(SUM(total_likes), 0) AS likes
            FROM orders
            WHERE license_key=?
        """, (row["license_key"],)).fetchone()

    return jsonify({
        "ok": True,
        "total_orders": stats["orders"],
        "total_likes": stats["likes"],
        "used_today": used_uids(row["license_key"])
    })


@app.route("/customer/like", methods=["POST"])
def customer_like():
    auth_error = require_customer()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}

    telegram_id = data.get("telegram_id")
    uid = str(data.get("uid", "")).strip()
    region = str(data.get("region", "")).upper()

    if not telegram_id or not uid.isdigit() or region not in ("BD", "IND"):
        return jsonify({"ok": False, "message": "Invalid UID or region"}), 400

    row, error = get_active_license(telegram_id)

    if error:
        return jsonify({"ok": False, "message": error})

    key = row["license_key"]
    today = quota_day()

    with connect_db() as db:
        already_used = db.execute("""
            SELECT 1 FROM uid_usage
            WHERE license_key=? AND quota_date=? AND uid=?
        """, (key, today, uid)).fetchone()

        if not already_used and row["daily_limit"] > 0:
            used = db.execute("""
                SELECT COUNT(*) AS n FROM uid_usage
                WHERE license_key=? AND quota_date=?
            """, (key, today)).fetchone()["n"]

            if used >= row["daily_limit"]:
                return jsonify({
                    "ok": False,
                    "message": "Daily UID limit reached"
                })

        api_names = json.loads(row["apis"])

        settings_rows = db.execute(
            "SELECT * FROM api_settings"
        ).fetchall()

        settings = {r["api_name"]: dict(r) for r in settings_rows}

    results = {}
    total = 0
    player_name = "Unknown"
    before = None
    after = None

    for api_name in api_names:
        result = call_selected_api(
            api_name, settings.get(api_name, {}),
            uid, region
        )

        results[api_name] = {
            "success": result.get("success", False),
            "likes": result.get("likes", 0),
            "error": result.get("error")
        }

        total += result.get("likes", 0)

        if result.get("name") and result["name"] != "Unknown":
            player_name = result["name"]

        if result.get("before") is not None:
            before = result["before"]

        if result.get("after") is not None:
            after = result["after"]

    if total <= 0:
        return jsonify({
            "ok": False,
            "message": "All selected APIs failed or returned zero likes",
            "apis": results
        })

    # Successful unique UID consumes one daily quota slot.
    with connect_db() as db:
        db.execute("""
            INSERT OR IGNORE INTO uid_usage
            (license_key, quota_date, uid)
            VALUES (?, ?, ?)
        """, (key, today, uid))

        db.execute("""
            INSERT INTO orders
            (license_key, telegram_id, uid, region,
             total_likes, result, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            key, telegram_id, uid, region, total,
            json.dumps(results),
            now_dhaka().isoformat()
        ))

    return jsonify({
        "ok": True,
        "name": player_name,
        "before": before,
        "after": after,
        "total_added": total,
        "apis": results,
        "quota_used": used_uids(key),
        "quota_date": today
    })


# =============== HEALTH CHECK ===============

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "online",
        "message": "Free Fire License Server is running"
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    if not MASTER_SECRET or not CUSTOMER_SECRET:
        raise RuntimeError(
            "Set MASTER_SECRET and CUSTOMER_SECRET environment variables"
        )

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
