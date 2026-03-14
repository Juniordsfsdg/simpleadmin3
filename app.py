import os, json, secrets, time, hashlib
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify)
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════
SECRET_KEY         = os.getenv("SECRET_KEY",       secrets.token_hex(32))
ROBLOX_API_KEY     = os.getenv("ROBLOX_API_KEY",   "")
ROBLOX_GROUP_ID    = int(os.getenv("ROBLOX_GROUP_ID",    "0"))
ROBLOX_UNIVERSE_ID = int(os.getenv("ROBLOX_UNIVERSE_ID", "0"))
GAME_API_SECRET    = os.getenv("GAME_API_SECRET",  "changeme_game_secret")
PORT               = int(os.getenv("PORT",         "5000"))

DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1482154749158490295/ZhU5CpI25alWtsEiF_dKI7m7NlePgew969fHcD4pNqoK5fm6xX0wBhsWXBMyZWM8xXc3"

# ── Staff Accounts ───────────────────────────────────────────────────────────
# Format: "username": {"password": "plaintext", "level": 1-5}
# Level: 2=Moderator 3=Admin 4=Owner 5=Developer
STAFF_ACCOUNTS = {
    os.getenv("ADMIN_USERNAME", "admin"): {
        "password": os.getenv("ADMIN_PASSWORD", "changeme"),
        "level": 5,
    },
    # Add more staff below:
    # "StaffName2": {"password": "theirpassword", "level": 3},
}

LEVEL_NAMES = {0:"Guest", 1:"Donator", 2:"Moderator", 3:"Admin", 4:"Owner", 5:"Developer"}

# ═══════════════════════════════════════════════════════
#  STATE
# ═══════════════════════════════════════════════════════
active_players:  list[dict] = []
synced_commands: list[dict] = []
action_log:      list[dict] = []
ban_list:        list[dict] = []
server_stats:    dict       = {}

# ═══════════════════════════════════════════════════════
#  FLASK
# ═══════════════════════════════════════════════════════
app = Flask(__name__)
app.secret_key = SECRET_KEY

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def log_action(action, target, reason="", moderator=""):
    action_log.insert(0, {
        "action":    action,
        "target":    target,
        "reason":    reason,
        "moderator": moderator or session.get("username", "system"),
        "discord":   session.get("discord", ""),
        "time":      datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    })
    if len(action_log) > 500:
        action_log.pop()

# ═══════════════════════════════════════════════════════
#  DISCORD WEBHOOK
# ═══════════════════════════════════════════════════════
def send_webhook(title, description, color=0x00d4ff, fields=None):
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "SimpleAdmin Dashboard"},
    }
    if fields:
        embed["fields"] = fields
    try:
        requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=5)
    except:
        pass

# ═══════════════════════════════════════════════════════
#  ROBLOX API HELPERS
# ═══════════════════════════════════════════════════════
def _oc():
    return {"x-api-key": ROBLOX_API_KEY, "Content-Type": "application/json"}

def rbx_get_user_by_name(username):
    r = requests.post("https://users.roblox.com/v1/usernames/users",
                      json={"usernames": [username], "excludeBannedUsers": False}, timeout=8)
    d = r.json().get("data", []) if r.ok else []
    return d[0] if d else None

def rbx_get_user_info(uid):
    r = requests.get(f"https://users.roblox.com/v1/users/{uid}", timeout=8)
    return r.json() if r.ok else None

def rbx_search_users(query):
    r = requests.get(f"https://users.roblox.com/v1/users/search?keyword={query}&limit=10", timeout=8)
    return r.json().get("data", []) if r.ok else []

def rbx_send_message(topic, payload):
    if not ROBLOX_UNIVERSE_ID:
        return False, "ROBLOX_UNIVERSE_ID not configured."
    r = requests.post(
        f"https://apis.roblox.com/messaging-service/v1/universes/{ROBLOX_UNIVERSE_ID}/topics/{topic}",
        headers=_oc(), json={"message": json.dumps(payload)}, timeout=8)
    return (True, "Signal sent.") if r.ok else (False, f"HTTP {r.status_code}")

def rbx_remove_from_group(uid):
    s = requests.get(
        f"https://apis.roblox.com/cloud/v2/groups/{ROBLOX_GROUP_ID}/memberships?filter=user=={uid}",
        headers=_oc(), timeout=8)
    if not s.ok: return False, f"HTTP {s.status_code}"
    mems = s.json().get("groupMemberships", [])
    if not mems: return False, "Not a group member."
    r = requests.delete(f"https://apis.roblox.com/cloud/v2/{mems[0]['path']}", headers=_oc(), timeout=8)
    return (True, "Removed from group.") if r.status_code in (200, 204) else (False, f"HTTP {r.status_code}")

def rbx_set_rank(uid, rank_id):
    s = requests.get(
        f"https://apis.roblox.com/cloud/v2/groups/{ROBLOX_GROUP_ID}/memberships?filter=user=={uid}",
        headers=_oc(), timeout=8)
    if not s.ok: return False, f"HTTP {s.status_code}"
    mems = s.json().get("groupMemberships", [])
    if not mems: return False, "Not a group member."
    p = requests.patch(f"https://apis.roblox.com/cloud/v2/{mems[0]['path']}",
                       headers=_oc(),
                       json={"role": f"groups/{ROBLOX_GROUP_ID}/roles/{rank_id}"}, timeout=8)
    return (True, "Rank updated.") if p.ok else (False, f"HTTP {p.status_code}")

# ═══════════════════════════════════════════════════════
#  AUTH ROUTES
# ═══════════════════════════════════════════════════════
@app.route("/", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        discord  = request.form.get("discord",  "").strip()

        if not discord:
            error = "Discord username is required."
        elif username in STAFF_ACCOUNTS and STAFF_ACCOUNTS[username]["password"] == password:
            level = STAFF_ACCOUNTS[username]["level"]
            session["logged_in"] = True
            session["username"]  = username
            session["discord"]   = discord
            session["level"]     = level

            # Send login notification to Discord webhook
            send_webhook(
                title="🔐 Staff Login",
                description=f"A staff member has logged into the SimpleAdmin dashboard.",
                color=0x00d4ff,
                fields=[
                    {"name": "Admin Username", "value": username,                         "inline": True},
                    {"name": "Discord",        "value": discord,                          "inline": True},
                    {"name": "Level",          "value": f"{level} — {LEVEL_NAMES.get(level,'?')}", "inline": True},
                    {"name": "Time",           "value": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), "inline": False},
                ]
            )
            log_action("LOGIN", username, f"Discord: {discord}", username)
            return redirect(url_for("dashboard"))
        else:
            error = "Invalid username or password."
            # Log failed attempt to webhook
            send_webhook(
                title="⚠️ Failed Login Attempt",
                description=f"Someone tried to log in with incorrect credentials.",
                color=0xff4757,
                fields=[
                    {"name": "Attempted Username", "value": username or "(blank)", "inline": True},
                    {"name": "Discord Provided",   "value": discord  or "(blank)", "inline": True},
                    {"name": "Time", "value": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), "inline": False},
                ]
            )

    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    uname   = session.get("username", "?")
    discord = session.get("discord",  "?")
    send_webhook(
        title="🚪 Staff Logout",
        description=f"A staff member logged out of the dashboard.",
        color=0xffd600,
        fields=[
            {"name": "Admin Username", "value": uname,   "inline": True},
            {"name": "Discord",        "value": discord, "inline": True},
        ]
    )
    session.clear()
    return redirect(url_for("login"))

# ═══════════════════════════════════════════════════════
#  DASHBOARD PAGES
# ═══════════════════════════════════════════════════════
@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html",
        username       = session.get("username"),
        discord        = session.get("discord"),
        level          = session.get("level", 0),
        level_name     = LEVEL_NAMES.get(session.get("level", 0), "?"),
        player_count   = len(active_players),
        ban_count      = len(ban_list),
        cmd_count      = len(synced_commands),
        recent_actions = action_log[:10],
    )

@app.route("/players")
@login_required
def players():
    return render_template("players.html",
        username  = session.get("username"),
        discord   = session.get("discord"),
        level     = session.get("level", 0),
        commands  = synced_commands,
    )

@app.route("/bans")
@login_required
def bans():
    return render_template("bans.html",
        username = session.get("username"),
        discord  = session.get("discord"),
        level    = session.get("level", 0),
        bans     = ban_list,
    )

@app.route("/logs")
@login_required
def logs():
    return render_template("logs.html",
        username = session.get("username"),
        discord  = session.get("discord"),
        level    = session.get("level", 0),
        logs     = action_log,
    )

@app.route("/commands")
@login_required
def commands_page():
    return render_template("commands.html",
        username  = session.get("username"),
        discord   = session.get("discord"),
        level     = session.get("level", 0),
        commands  = synced_commands,
    )

@app.route("/search")
@login_required
def search():
    return render_template("search.html",
        username = session.get("username"),
        discord  = session.get("discord"),
        level    = session.get("level", 0),
    )

# ═══════════════════════════════════════════════════════
#  ACTION API
# ═══════════════════════════════════════════════════════
@app.route("/api/kick", methods=["POST"])
@login_required
def api_kick():
    d      = request.get_json(force=True)
    uid    = d.get("userId")
    uname  = d.get("username", "?")
    reason = d.get("reason", "Kicked by admin")
    ok, msg = rbx_send_message("SimpleAdmin_Web", {
        "action": "Kick", "userId": uid, "username": uname,
        "reason": reason, "moderator": session.get("username"),
    })
    log_action("KICK", uname, reason)
    send_webhook("👢 Player Kicked", f"**{uname}** was kicked.",
        color=0xff7c2a,
        fields=[
            {"name": "Player",    "value": uname,                      "inline": True},
            {"name": "Reason",    "value": reason,                     "inline": True},
            {"name": "Moderator", "value": session.get("username","?"),"inline": True},
            {"name": "Discord",   "value": session.get("discord","?"), "inline": True},
        ])
    return jsonify({"ok": ok, "message": msg})

@app.route("/api/ban", methods=["POST"])
@login_required
def api_ban():
    d      = request.get_json(force=True)
    uid    = d.get("userId")
    uname  = d.get("username", "?")
    reason = d.get("reason", "Banned")
    g_ok,  g_msg  = rbx_remove_from_group(uid) if uid else (False, "No userId")
    gm_ok, gm_msg = rbx_send_message("SimpleAdmin_Web", {
        "action": "Ban", "userId": uid, "username": uname,
        "reason": reason, "moderator": session.get("username"),
    })
    ban_list.insert(0, {
        "userId":    uid,
        "username":  uname,
        "reason":    reason,
        "moderator": session.get("username"),
        "discord":   session.get("discord"),
        "time":      datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    })
    log_action("BAN", uname, reason)
    send_webhook("🔨 Player Banned", f"**{uname}** was banned.",
        color=0xff3d5a,
        fields=[
            {"name": "Player",    "value": uname,                      "inline": True},
            {"name": "User ID",   "value": str(uid),                   "inline": True},
            {"name": "Reason",    "value": reason,                     "inline": False},
            {"name": "Moderator", "value": session.get("username","?"),"inline": True},
            {"name": "Discord",   "value": session.get("discord","?"), "inline": True},
        ])
    return jsonify({"ok": g_ok or gm_ok, "message": f"Group: {g_msg} | Game: {gm_msg}"})

@app.route("/api/unban", methods=["POST"])
@login_required
def api_unban():
    global ban_list
    d     = request.get_json(force=True)
    uid   = d.get("userId")
    uname = d.get("username", "?")
    ban_list = [b for b in ban_list if b.get("userId") != uid]
    rbx_send_message("SimpleAdmin_Web", {"action": "Unban", "userId": uid, "username": uname})
    log_action("UNBAN", uname)
    send_webhook("✅ Player Unbanned", f"**{uname}** was unbanned.",
        color=0x00e676,
        fields=[
            {"name": "Player",    "value": uname,                      "inline": True},
            {"name": "Moderator", "value": session.get("username","?"),"inline": True},
            {"name": "Discord",   "value": session.get("discord","?"), "inline": True},
        ])
    return jsonify({"ok": True, "message": "Unbanned."})

@app.route("/api/command", methods=["POST"])
@login_required
def api_command():
    d   = request.get_json(force=True)
    cmd = d.get("command", "")
    uid = d.get("userId")
    uname = d.get("username", "?")
    ok, msg = rbx_send_message("SimpleAdmin_Web", {
        "action": "RunCommand", "command": cmd,
        "userId": uid, "username": uname,
        "args": d.get("args", {}), "moderator": session.get("username"),
    })
    log_action(f"CMD:{cmd}", uname, str(d.get("args", {})))
    return jsonify({"ok": ok, "message": msg})

@app.route("/api/rank", methods=["POST"])
@login_required
def api_rank():
    d = request.get_json(force=True)
    ok, msg = rbx_set_rank(d.get("userId"), d.get("rankId"))
    log_action("RANK", d.get("username","?"), f"Rank ID {d.get('rankId')}")
    return jsonify({"ok": ok, "message": msg})

@app.route("/api/search_user", methods=["POST"])
@login_required
def api_search_user():
    d     = request.get_json(force=True)
    query = d.get("query", "")
    # Try exact username first
    user = rbx_get_user_by_name(query)
    if user:
        info = rbx_get_user_info(user["id"])
        # Check if banned
        is_banned = any(b.get("userId") == user["id"] for b in ban_list)
        # Find in active players
        in_game = next((p for p in active_players if p.get("userId") == user["id"]), None)
        # Get recent actions
        user_actions = [a for a in action_log if query.lower() in (a.get("target","")).lower()][:10]
        return jsonify({
            "ok": True,
            "user": {
                "id":          user["id"],
                "name":        info.get("name","?") if info else user.get("name","?"),
                "displayName": info.get("displayName","?") if info else "?",
                "description": info.get("description","") if info else "",
                "created":     info.get("created","?") if info else "?",
                "isBanned":    is_banned,
                "inGame":      in_game is not None,
                "gameData":    in_game,
                "recentActions": user_actions,
            }
        })
    return jsonify({"ok": False, "message": f"User '{query}' not found."})

# ═══════════════════════════════════════════════════════
#  GAME INBOUND API
# ═══════════════════════════════════════════════════════
def require_game_secret(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.headers.get("X-SA-Secret","") != GAME_API_SECRET:
            return jsonify({"ok": False}), 403
        return f(*args, **kwargs)
    return decorated

@app.route("/game/heartbeat", methods=["POST"])
@require_game_secret
def game_heartbeat():
    global active_players, server_stats
    data           = request.get_json(force=True)
    active_players = data.get("players", [])
    server_stats   = data.get("stats", {})
    return jsonify({"ok": True})

@app.route("/game/commands", methods=["POST"])
@require_game_secret
def game_commands():
    global synced_commands
    synced_commands = request.get_json(force=True).get("commands", [])
    return jsonify({"ok": True, "received": len(synced_commands)})

@app.route("/game/log", methods=["POST"])
@require_game_secret
def game_log():
    data = request.get_json(force=True)
    action_log.insert(0, {
        "action":    data.get("command", "?"),
        "target":    data.get("target",  "?"),
        "reason":    data.get("reason",  ""),
        "moderator": data.get("moderator","in-game"),
        "discord":   "in-game",
        "time":      data.get("time", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
    })
    return jsonify({"ok": True})

# ═══════════════════════════════════════════════════════
#  DATA API
# ═══════════════════════════════════════════════════════
@app.route("/api/players")
@login_required
def api_players():
    return jsonify({"players": active_players, "stats": server_stats})

@app.route("/api/stats")
@login_required
def api_stats():
    return jsonify({
        "players":  len(active_players),
        "bans":     len(ban_list),
        "commands": len(synced_commands),
        "logs":     len(action_log),
    })

if __name__ == "__main__":
    print(f"[SimpleAdmin] Running on http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
