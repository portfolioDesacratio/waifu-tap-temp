"""
Waifu Tap — Flask Backend (async routes, для PythonAnywhere)
"""
import os, sys, json, hashlib, hmac, math, asyncio
from datetime import datetime
from functools import wraps

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import config
from backend.database import (
    init_db_path, init_db, get_db,
    get_or_create_user, get_user_by_telegram_id,
    process_tap, get_shop_items, buy_item,
    get_leaderboard, get_all_waifus, waifu_unlock,
    claim_daily_reward, get_daily_status, get_user_inventory,
    seed_waifus, seed_shop,
)

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder=None)
CORS(app)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
ASSETS_DIR = os.path.join(FRONTEND_DIR, "assets")

# Инициализируем БД
os.makedirs(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"), exist_ok=True)
init_db_path(config.DB_PATH)
asyncio.run(init_db())

# ─── Хелпер: async → sync через выделенный event loop ───
import concurrent.futures

def async_to_sync(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            loop = asyncio.get_running_loop()
            # Уже есть loop → запускаем в отдельном потоке
            with concurrent.futures.ThreadPoolExecutor() as pool:
                fut = pool.submit(lambda: asyncio.run(f(*args, **kwargs)))
                return fut.result()
        except RuntimeError:
            # Нет loop → можно спокойно run
            return asyncio.run(f(*args, **kwargs))
    return wrapper


# ─── Валидация Telegram ───
def validate_telegram_data(init_data: str) -> dict | None:
    try:
        from urllib.parse import parse_qs
        parsed = parse_qs(init_data)
        data_dict = {k: v[0] for k, v in parsed.items()}
        hash_received = data_dict.pop("hash", None)
        if not hash_received:
            return None
        sorted_items = sorted(data_dict.items())
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_items)
        secret_key = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
        hash_calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if hash_calculated != hash_received:
            return None
        user_json = data_dict.get("user", "{}")
        return json.loads(user_json)
    except:
        return None


# ─── Админ ───
def is_admin(tid):
    return tid and int(tid) == config.ADMIN_ID


# ─── Routes ───

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})


@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/assets/<path:subpath>")
def serve_assets(subpath):
    return send_from_directory(ASSETS_DIR, subpath)


@app.route("/api/auth", methods=["POST"])
@async_to_sync
async def api_auth():
    try:
        data = request.get_json(silent=True) or {}
        init_data = data.get("initData", "")
        referrer_id = data.get("referrerId")
        user_data = validate_telegram_data(init_data)
        if not user_data:
            return jsonify({"success": False, "error": "Invalid auth data"}), 401
        telegram_id = user_data.get("id")
        first_name = user_data.get("first_name", "")
        username = user_data.get("username", "")
        user = await get_or_create_user(telegram_id, first_name, username, referrer_id)
        return jsonify({"success": True, "user": user})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/guest/auth", methods=["POST"])
@async_to_sync
async def api_guest_auth():
    try:
        data = request.get_json(silent=True) or {}
        guest_id = data.get("guest_id", "guest_unknown")
        name = data.get("name", "Гость")
        telegram_id = int(hashlib.md5(guest_id.encode()).hexdigest()[:8], 16)
        user = await get_or_create_user(telegram_id, name, f"guest_{guest_id[:8]}")
        # Даём стартовые монеты и энергию для теста
        if user["coins"] == 0:
            db = await get_db()
            try:
                await db.execute(
                    "UPDATE users SET coins = 5000, max_energy = 500, energy = 500, energy_regen_level = 2 WHERE id = ?",
                    (user["id"],)
                )
                await db.commit()
                user["coins"] = 5000
                user["max_energy"] = 500
                user["energy"] = 500
                user["energy_regen_level"] = 2
            finally:
                await db.close()
        return jsonify({"success": True, "user": user, "is_guest": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/bot/register", methods=["POST"])
@async_to_sync
async def api_bot_register():
    """Регистрация пользователя от имени бота (без валидации Telegram)"""
    try:
        data = request.get_json(silent=True) or {}
        telegram_id = data.get("telegram_id")
        first_name = data.get("first_name", "")
        username = data.get("username", "")
        referrer_id = data.get("referrerId")
        if not telegram_id:
            return jsonify({"success": False, "error": "Missing telegram_id"}), 400
        user = await get_or_create_user(telegram_id, first_name, username, referrer_id)
        return jsonify({"success": True, "user": user})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/user/<int:telegram_id>")
@async_to_sync
async def api_user(telegram_id):
    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404
    safe_user = {k: v for k, v in user.items() if k != "id"}
    return jsonify({"success": True, "user": safe_user})


@app.route("/api/tap", methods=["POST"])
@async_to_sync
async def api_tap():
    try:
        data = request.get_json(silent=True) or {}
        telegram_id = data.get("telegram_id")
        if not telegram_id:
            return jsonify({"success": False, "error": "telegram_id required"}), 400
        result = await process_tap(telegram_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/tap/batch", methods=["POST"])
@async_to_sync
async def api_tap_batch():
    try:
        data = request.get_json(silent=True) or {}
        telegram_id = data.get("telegram_id")
        count = min(int(data.get("count", 1)), 50)
        if not telegram_id:
            return jsonify({"success": False, "error": "telegram_id required"}), 400
        total_earned = 0
        taps_done = 0
        energy = 0
        max_energy = 100
        combo = 0
        for _ in range(count):
            result = await process_tap(telegram_id)
            if result.get("success"):
                total_earned += result.get("coins_earned", 0)
                taps_done += 1
                energy = result.get("energy", 0)
                max_energy = result.get("max_energy", 100)
                combo = result.get("combo", 0)
            else:
                energy = result.get("energy", 0)
                break
        return jsonify({
            "success": True,
            "taps": taps_done,
            "total_earned": round(total_earned, 1),
            "energy": energy,
            "max_energy": max_energy,
            "combo": combo,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/shop")
@async_to_sync
async def api_shop():
    category = request.args.get("category")
    items = await get_shop_items(category)
    return jsonify({"success": True, "items": items})


@app.route("/api/shop/buy", methods=["POST"])
@async_to_sync
async def api_shop_buy():
    try:
        data = request.get_json(silent=True) or {}
        telegram_id = data.get("telegram_id")
        item_id = data.get("item_id")
        payment_method = data.get("payment_method", "coins")
        if not telegram_id or not item_id:
            return jsonify({"success": False, "error": "telegram_id and item_id required"}), 400
        result = await buy_item(telegram_id, item_id, payment_method)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/leaderboard")
@async_to_sync
async def api_leaderboard():
    limit = request.args.get("limit", 50, type=int)
    top = await get_leaderboard(limit)
    waifus = await get_all_waifus()
    waifu_map = {w["id"]: {"name": w["name"], "emoji": w["emoji"]} for w in waifus}
    result = []
    for i, u in enumerate(top, 1):
        waifu_info = waifu_map.get(u["current_waifu_id"], {"name": "—", "emoji": "🌸"})
        result.append({
            "rank": i,
            "telegram_id": u["telegram_id"],
            "name": u["first_name"] or u["username"] or f"User {u['telegram_id']}",
            "coins": u["coins"],
            "total_taps": u["total_taps"],
            "waifu": waifu_info,
        })
    return jsonify({"success": True, "leaderboard": result})


@app.route("/api/daily/claim", methods=["POST"])
@async_to_sync
async def api_daily_claim():
    try:
        data = request.get_json(silent=True) or {}
        telegram_id = data.get("telegram_id")
        if not telegram_id:
            return jsonify({"success": False, "error": "telegram_id required"}), 400
        result = await claim_daily_reward(telegram_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/daily/status/<int:telegram_id>")
@async_to_sync
async def api_daily_status(telegram_id):
    status = await get_daily_status(telegram_id)
    return jsonify({"success": True, **status})


@app.route("/api/inventory/<int:telegram_id>")
@async_to_sync
async def api_inventory(telegram_id):
    items = await get_user_inventory(telegram_id)
    return jsonify({"success": True, "inventory": items})


@app.route("/api/waifus")
@async_to_sync
async def api_waifus():
    all_waifus = await get_all_waifus()
    return jsonify({"success": True, "waifus": all_waifus})


@app.route("/api/waifu/select", methods=["POST"])
@async_to_sync
async def api_waifu_select():
    try:
        data = request.get_json(silent=True) or {}
        telegram_id = data.get("telegram_id")
        waifu_id = data.get("waifu_id")
        if not telegram_id or not waifu_id:
            return jsonify({"success": False, "error": "telegram_id and waifu_id required"}), 400
        result = await waifu_unlock(telegram_id, waifu_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/referral/<int:telegram_id>")
@async_to_sync
async def api_referral(telegram_id):
    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?",
            (user["id"],)
        )
        referral_count = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT SUM(bonus_coins) FROM referrals WHERE referrer_id = ?",
            (user["id"],)
        )
        total_bonus = (await cursor.fetchone())[0] or 0
        return jsonify({
            "success": True,
            "referral_count": referral_count,
            "total_bonus": total_bonus,
            "referral_link": f"https://t.me/{config.BOT_USERNAME}?start=ref_{telegram_id}",
        })
    finally:
        await db.close()


# ─── ADMIN API ───

@app.route("/api/admin/addcoins", methods=["POST"])
@async_to_sync
async def api_admin_addcoins():
    try:
        data = request.get_json(silent=True) or {}
        admin_id = data.get("admin_id")
        if admin_id != config.ADMIN_ID:
            return jsonify({"success": False, "error": "Access denied"}), 403
        telegram_id = data.get("telegram_id")
        amount = float(data.get("amount", 0))
        if amount <= 0:
            return jsonify({"success": False, "error": "Amount must be positive"})
        user = await get_user_by_telegram_id(telegram_id)
        if not user:
            return jsonify({"success": False, "error": "User not found"})
        db = await get_db()
        try:
            await db.execute(
                "UPDATE users SET coins = coins + ?, total_coins_earned = total_coins_earned + ? WHERE id = ?",
                (amount, amount, user["id"])
            )
            await db.execute(
                """INSERT INTO transactions (user_id, type, amount, currency, description)
                VALUES (?, 'admin_gift', ?, 'coins', 'Начислено администратором')""",
                (user["id"], amount)
            )
            await db.commit()
        finally:
            await db.close()
        return jsonify({"success": True, "new_balance": user["coins"] + amount})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/addstars", methods=["POST"])
@async_to_sync
async def api_admin_addstars():
    try:
        data = request.get_json(silent=True) or {}
        admin_id = data.get("admin_id")
        if admin_id != config.ADMIN_ID:
            return jsonify({"success": False, "error": "Access denied"}), 403
        telegram_id = data.get("telegram_id")
        amount = int(data.get("amount", 0))
        if amount <= 0:
            return jsonify({"success": False, "error": "Amount must be positive"})
        user = await get_user_by_telegram_id(telegram_id)
        if not user:
            return jsonify({"success": False, "error": "User not found"})
        db = await get_db()
        try:
            await db.execute(
                "UPDATE users SET stars = stars + ?, total_stars_earned = total_stars_earned + ? WHERE id = ?",
                (amount, amount, user["id"])
            )
            await db.execute(
                """INSERT INTO transactions (user_id, type, amount, currency, description)
                VALUES (?, 'admin_gift', ?, 'stars', 'Начислено администратором')""",
                (user["id"], amount)
            )
            await db.commit()
        finally:
            await db.close()
        return jsonify({"success": True, "new_balance": user["stars"] + amount})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/admin/stats")
@async_to_sync
async def api_admin_stats():
    admin_id = request.args.get("admin_id") or (request.get_json(silent=True) or {}).get("admin_id")
    if int(admin_id or 0) != config.ADMIN_ID:
        return jsonify({"success": False, "error": "Access denied"}), 403
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        total_users = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COALESCE(SUM(coins), 0) FROM users")
        total_coins = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COALESCE(SUM(total_taps), 0) FROM users")
        total_taps = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COALESCE(SUM(stars), 0) FROM users")
        total_stars = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM referrals")
        total_referrals = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM users WHERE last_activity >= datetime('now', '-1 day')")
        active_today = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM users WHERE last_activity >= datetime('now', '-7 days')")
        active_week = (await cursor.fetchone())[0]
        return jsonify({
            "success": True,
            "stats": {
                "total_users": total_users,
                "total_coins": total_coins,
                "total_taps": total_taps,
                "total_stars": total_stars,
                "total_referrals": total_referrals,
                "active_today": active_today,
                "active_week": active_week,
            }
        })
    finally:
        await db.close()


@app.route("/api/admin/broadcast", methods=["POST"])
@async_to_sync
async def api_admin_broadcast():
    try:
        data = request.get_json(silent=True) or {}
        admin_id = data.get("admin_id")
        if admin_id != config.ADMIN_ID:
            return jsonify({"success": False, "error": "Access denied"}), 403
        text = data.get("text", "")
        db = await get_db()
        try:
            cursor = await db.execute("SELECT COUNT(*) FROM users")
            total = (await cursor.fetchone())[0]
            print(f"[BROADCAST] Admin {admin_id}: {text[:50]}... to {total} users")
            return jsonify({
                "success": True,
                "sent": total,
                "message": "Рассылка запущена. В продакшене добавить очередь."
            })
        finally:
            await db.close()
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─── WSGI entry (для PythonAnywhere) ───
application = app

if __name__ == "__main__":
    port = int(os.getenv("PORT", config.PORT))
    print(f"🚀 Waifu Tap API on http://{config.HOST}:{port}")
    app.run(host=config.HOST, port=port, debug=False)
