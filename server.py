import hashlib
import json
import os
import secrets
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from fastapi import Cookie, FastAPI, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from paho.mqtt import client as mqtt

# ---- Auth config ----
AUTH_PIN = os.getenv("AUTH_PIN", "1234")
AUTH_COOKIE_NAME = "evse_auth"
AUTH_COOKIE_MAX_AGE = 7 * 24 * 60 * 60  # 7 days in seconds

# Generate a secret for signing tokens (persists across restarts via file)
AUTH_SECRET_FILE = ".auth_secret"
def _get_auth_secret() -> str:
    if os.path.exists(AUTH_SECRET_FILE):
        with open(AUTH_SECRET_FILE, "r") as f:
            return f.read().strip()
    secret = secrets.token_hex(32)
    with open(AUTH_SECRET_FILE, "w") as f:
        f.write(secret)
    return secret

AUTH_SECRET = _get_auth_secret()

def _generate_auth_token() -> str:
    """Generate a signed auth token."""
    data = f"{AUTH_PIN}:{AUTH_SECRET}"
    return hashlib.sha256(data.encode()).hexdigest()

def _verify_auth_token(token: str | None) -> bool:
    """Verify the auth token from cookie."""
    if not token:
        return False
    return token == _generate_auth_token()

SERIAL = "0166341352572539"
BASE = f"evseMQTT/{SERIAL}"

# ---- Command topic + payloads ----
# evseMQTT doesn't document payloads clearly; so we make them configurable.
CMD_TOPIC = os.getenv("CMD_TOPIC", f"{BASE}/command")
STOP_PAYLOAD = '{"charge_state": 0}'
# Template: use {amps} placeholder
AMPS_PAYLOAD_TEMPLATE = '{"charge_amps": {amps}}'

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

# ---- Session logging config ----
SESSIONS_FILE = os.getenv("SESSIONS_FILE", "sessions.json")
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "500"))

# ---- Settings config ----
SETTINGS_FILE = os.getenv("SETTINGS_FILE", "settings.json")

# ---- Telegram notifications ----
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

def _send_telegram(message: str):
    """Send a message via Telegram bot (non-blocking)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    
    def _send():
        try:
            import urllib.request
            import urllib.parse
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            }).encode()
            req = urllib.request.Request(url, data=data)
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"Telegram error: {e}")
    
    # Send in background thread to not block
    t = threading.Thread(target=_send, daemon=True)
    t.start()

# ---- Templates directory ----
TEMPLATES_DIR = Path(__file__).parent / "templates"

# ---- Cost calculation ----
DEFAULT_CLOCK_DISCOUNT = 20  # 20% off during clock hours

def _is_minute_in_clock(minute: int, clock_start: str, clock_end: str) -> bool:
    """Check if a minute-of-day is within clock hours."""
    start_h, start_m = map(int, clock_start.split(':'))
    end_h, end_m = map(int, clock_end.split(':'))
    start_mins = start_h * 60 + start_m
    end_mins = end_h * 60 + end_m
    
    if start_mins <= end_mins:
        return minute >= start_mins and minute < end_mins
    else:
        # Overnight range
        return minute >= start_mins or minute < end_mins

def _calc_session_cost(energy: float, started_at: str, ended_at: str) -> float:
    """Calculate session cost with clock discount."""
    if energy <= 0:
        return 0.0
    
    price_per_kwh = app_settings.get("price_per_kwh", 0.55)
    clock_start = app_settings.get("clock_start", "23:00")
    clock_end = app_settings.get("clock_end", "07:00")
    
    try:
        start_dt = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
        end_dt = datetime.fromisoformat(ended_at.replace('Z', '+00:00'))
    except:
        return energy * price_per_kwh
    
    total_mins = max(1, int((end_dt - start_dt).total_seconds() / 60))
    
    # Sample every 5 minutes for speed
    clock_mins = 0
    step = max(1, total_mins // 100)
    samples = 0
    
    for i in range(0, total_mins, step):
        sample_time = start_dt + __import__('datetime').timedelta(minutes=i)
        # Convert to Jerusalem time
        jerusalem_hour = (sample_time.hour + 2) % 24  # Rough UTC+2 approximation
        minute_of_day = jerusalem_hour * 60 + sample_time.minute
        
        if _is_minute_in_clock(minute_of_day, clock_start, clock_end):
            clock_mins += step
        samples += 1
    
    clock_mins = min(clock_mins, total_mins)
    non_clock_mins = total_mins - clock_mins
    
    # Split energy proportionally
    clock_energy = energy * (clock_mins / total_mins) if total_mins > 0 else 0
    non_clock_energy = energy * (non_clock_mins / total_mins) if total_mins > 0 else energy
    
    # Apply discount to clock hours
    discount_percent = app_settings.get("clock_discount_percent", DEFAULT_CLOCK_DISCOUNT)
    discount_multiplier = 1 - (discount_percent / 100)
    cost = (clock_energy * price_per_kwh * discount_multiplier) + (non_clock_energy * price_per_kwh)
    return cost

def _load_settings() -> dict:
    defaults = {
        "clock_start": "07:00",
        "clock_end": "23:00",
        "users": ["User"],
        "price_per_kwh": 0.64,
        "clock_discount_percent": 20,  # 20% off during clock hours
        "battery_capacity_kwh": 64.0,  # MG4 default
    }
    needs_save = False

    if not os.path.exists(SETTINGS_FILE):
        # No file exists, use defaults and save them
        _save_settings(defaults)
        return defaults

    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Merge with defaults for any missing keys
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
                needs_save = True
        # Save back if we added missing keys
        if needs_save:
            _save_settings(data)
        return data
    except Exception:
        return defaults

def _save_settings(settings: dict):
    try:
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except Exception:
        pass

app_settings: dict = _load_settings()

app = FastAPI()

latest_charge: dict = {}
latest_config: dict = {}
availability: str = "unknown"
last_start_user: str | None = None
last_mqtt_update: float = 0.0  # timestamp of last MQTT message received

# Very small in‑memory session store, persisted to JSON.
sessions: list[dict] = []
current_session: dict | None = None
_sessions_lock = threading.Lock()


def _utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _load_sessions():
    global sessions, current_session
    if not os.path.exists(SESSIONS_FILE):
        sessions = []
        current_session = None
        return
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Backwards‑compat:
        #  - old format: plain list of sessions
        #  - new format: {"sessions": [...], "current_session": {...}}
        if isinstance(data, list):
            sessions = data
            current_session = None
        elif isinstance(data, dict):
            sessions = data.get("sessions") or []
            cs = data.get("current_session")
            current_session = cs if isinstance(cs, dict) else None
        else:
            sessions = []
            current_session = None
    except Exception:
        sessions = []
        current_session = None


def _save_sessions():
    # Called from MQTT thread; keep it simple and safe.
    try:
        tmp = SESSIONS_FILE + ".tmp"
        payload = {
            "sessions": sessions[-MAX_SESSIONS:],
            "current_session": current_session,
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SESSIONS_FILE)
    except Exception:
        # If disk write fails, don't crash the bridge.
        pass


def _check_for_missed_session(amount_val: float | None, ts: str):
    """
    Detect if a session happened while Pi was offline.
    Compare current_amount to the last session's end_amount_kwh.
    If there's a gap, create a ghost session to account for the missing energy.
    """
    global sessions

    if amount_val is None:
        return

    # Get the last completed session's end_amount_kwh
    if not sessions:
        return

    last_session = sessions[-1]
    last_end_amount = last_session.get("end_amount_kwh")

    if last_end_amount is None:
        return

    # Check for gap (with small tolerance for floating point)
    gap = amount_val - last_end_amount
    if gap < 0.01:  # Less than 0.01 kWh difference, no gap
        return

    # There's a gap! Create a ghost session
    last_ended_at = last_session.get("ended_at") or ts
    ghost_session = {
        "id": f"ghost-{int(time.time())}-{len(sessions)+1}",
        "started_at": last_ended_at,
        "ended_at": ts,
        "start_amount_kwh": last_end_amount,
        "end_amount_kwh": amount_val,
        "session_energy_kwh": gap,
        "meta": {
            "plug_state": None,
            "output_state": None,
            "current_state": None,
            "user": "Unknown (offline)",
        },
    }
    sessions.append(ghost_session)
    _save_sessions()


def _update_sessions_from_charge(charge: dict):
    """
    Very simple heuristic:
      - Treat a session as 'active' while current_energy > 0.
      - When it goes back to 0 (or None) after being active, close the session.
    We can refine this later once we see real payloads.
    """
    global current_session, sessions

    ts = _utc_iso()
    energy = charge.get("current_energy")
    amount = charge.get("current_amount")  # monotonically increasing counter

    # Normalize numeric values if they come in as strings
    try:
        energy_val = float(energy) if energy is not None else None
    except (TypeError, ValueError):
        energy_val = None
    try:
        amount_val = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount_val = None

    # Keep the simple "energy > 0" heuristic for active session for now.
    is_active = energy_val is not None and energy_val > 0

    with _sessions_lock:
        # Check for missed sessions while Pi was offline
        # Only check if: no active session, not currently charging, and we have amount data
        if current_session is None and not is_active and amount_val is not None:
            _check_for_missed_session(amount_val, ts)

        if current_session is None and is_active:
            # Start of a new session
            session_id = f"{int(time.time())}-{len(sessions)+1}"
            user = last_start_user or "Unknown"
            current_session = {
                "id": session_id,
                "started_at": ts,
                "ended_at": None,
                # energy accounting based solely on the monotonic amount counter
                "start_amount_kwh": amount_val,
                "end_amount_kwh": amount_val,
                "session_energy_kwh": None,
                "meta": {
                    "plug_state": charge.get("plug_state"),
                    "output_state": charge.get("output_state"),
                    "current_state": charge.get("current_state"),
                    "user": user,
                },
            }
            _save_sessions()
            # Telegram notification for session start
            _send_telegram(f"🔌 <b>Charging Started</b>\n👤 User: {user}")
        elif current_session is not None and not is_active:
            # End of an existing session
            current_session["ended_at"] = ts
            current_session["end_amount_kwh"] = amount_val

            # Session energy purely from the amount counter
            start_amt = current_session.get("start_amount_kwh")
            if start_amt is not None and amount_val is not None:
                session_energy = max(0.0, amount_val - start_amt)
                current_session["session_energy_kwh"] = session_energy
            else:
                session_energy = 0.0
                current_session["session_energy_kwh"] = None

            user = current_session.get("meta", {}).get("user", "Unknown")
            started_at = current_session.get("started_at", ts)
            sessions.append(current_session)
            current_session = None
            _save_sessions()
            # Telegram notification for session end
            battery_capacity = app_settings.get("battery_capacity_kwh", 64.0)
            battery_pct = round((session_energy / battery_capacity) * 100) if battery_capacity > 0 else 0
            session_cost = _calc_session_cost(session_energy, started_at, ts)
            _send_telegram(
                f"⚡ <b>Charging Complete!</b>\n"
                f"🔋 Energy: {session_energy:.1f} kWh (+{battery_pct}%)\n"
                f"💰 Cost: ₪{session_cost:.2f}\n"
                f"👤 User: {user}"
            )
        elif current_session is not None and is_active:
            # Update rolling values while charging
            current_session["end_amount_kwh"] = amount_val
            _save_sessions()


def on_connect(client, userdata, flags, rc):
    client.subscribe(f"{BASE}/availability")
    client.subscribe(f"{BASE}/state/charge")
    client.subscribe(f"{BASE}/state/config")


_last_error_notified: str | None = None

def on_message(client, userdata, msg):
    global latest_charge, latest_config, availability, last_mqtt_update, _last_error_notified
    topic = msg.topic
    payload = msg.payload.decode(errors="ignore")

    last_mqtt_update = time.time()

    if topic.endswith("/availability"):
        availability = payload.strip()
    elif topic.endswith("/state/charge"):
        try:
            latest_charge = json.loads(payload)
        except Exception:
            latest_charge = {"raw": payload}
        _update_sessions_from_charge(latest_charge)
        
        # Check for errors and send Telegram notification
        error_details = latest_charge.get("error_details", "")
        is_error = error_details and "no error" not in error_details.lower()
        if is_error and error_details != _last_error_notified:
            _send_telegram(f"⚠️ <b>Charger Error!</b>\n{error_details}")
            _last_error_notified = error_details
        elif not is_error:
            _last_error_notified = None
    elif topic.endswith("/state/config"):
        try:
            latest_config = json.loads(payload)
        except Exception:
            latest_config = {"raw": payload}


_load_sessions()

mqttc = mqtt.Client()
mqttc.on_connect = on_connect
mqttc.on_message = on_message
mqttc.connect(MQTT_HOST, MQTT_PORT, 60)
mqttc.loop_start()


def publish(payload: str):
    mqttc.publish(CMD_TOPIC, payload)


@app.get("/api/state")
def api_state():
    return {
        "availability": availability,
        "charge": latest_charge,
        "config": latest_config,
        "cmd_topic": CMD_TOPIC,
        "last_update": last_mqtt_update,
    }


@app.get("/api/sessions")
def api_sessions():
    # Return newest first; include current in‑progress session if present
    with _sessions_lock:
        items = sessions[-MAX_SESSIONS:]
        if current_session is not None:
            items = items + [current_session]
        return {"sessions": list(reversed(items))}


@app.post("/api/session/{session_id}/note")
def api_session_note(session_id: str, body: dict):
    """Add or update a note on a session."""
    note = body.get("note", "").strip()
    
    with _sessions_lock:
        # Check current session first
        if current_session is not None and current_session.get("id") == session_id:
            if "meta" not in current_session:
                current_session["meta"] = {}
            current_session["meta"]["note"] = note
            _save_sessions()
            return {"ok": True}
        
        # Check completed sessions
        for s in sessions:
            if s.get("id") == session_id:
                if "meta" not in s:
                    s["meta"] = {}
                s["meta"]["note"] = note
                _save_sessions()
                return {"ok": True}
    
    return {"ok": False, "error": "Session not found"}


@app.get("/api/settings")
def api_get_settings():
    return app_settings


@app.post("/api/settings")
def api_post_settings(new_settings: dict):
    global app_settings
    # Update only known keys
    if "clock_start" in new_settings:
        app_settings["clock_start"] = new_settings["clock_start"]
    if "clock_end" in new_settings:
        app_settings["clock_end"] = new_settings["clock_end"]
    if "users" in new_settings and isinstance(new_settings["users"], list):
        app_settings["users"] = new_settings["users"]
    if "price_per_kwh" in new_settings:
        try:
            app_settings["price_per_kwh"] = float(new_settings["price_per_kwh"])
        except (TypeError, ValueError):
            pass
    if "battery_capacity_kwh" in new_settings:
        try:
            app_settings["battery_capacity_kwh"] = float(new_settings["battery_capacity_kwh"])
        except (TypeError, ValueError):
            pass
    _save_settings(app_settings)
    return {"ok": True, "settings": app_settings}


def pause_ble_for(seconds: int):
    # stop bridge
    subprocess.run(["sudo", "systemctl", "stop", "evseMQTT"], check=False)

    # restart after delay
    def resume():
        subprocess.run(["sudo", "systemctl", "start", "evseMQTT"], check=False)

    t = threading.Timer(seconds, resume)
    t.daemon = True
    t.start()


@app.post("/api/pause_ble/{seconds}")
def api_pause_ble(seconds: int):
    seconds = max(5, min(seconds, 600))  # clamp 5s..10min for safety
    pause_ble_for(seconds)
    return {"ok": True, "paused_for": seconds}


@app.post("/api/telegram/test")
def api_telegram_test():
    """Send a test message to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"ok": False, "error": "Telegram not configured"}
    
    import random
    messages = [
        "🚗⚡ Beep boop! Your MG4 says hi! It's dreaming of electrons...",
        "🔌 Testing 1, 2, 3... Is this thing on? Your charger is ready to party!",
        "⚡ Shocking news! Your Telegram notifications are working! 🎉",
        "🔋 Your car whispered: 'Feed me electrons!' - Notification test successful!",
        "🚀 Houston, we have connection! Your charger is online and feeling electric!",
        "⚡ Plot twist: Your charger just sent you a message. Mind = blown! 🤯",
        "🔌 Your MG4 wanted to say: 'I love you more than gasoline!' 💚",
        "🎮 Achievement unlocked: Telegram notifications configured! +100 EV points!",
    ]
    message = random.choice(messages)
    _send_telegram(message)
    return {"ok": True, "message": message}


@app.post("/api/start")
def api_start():
    # Backwards‑compat: start without explicit user (records as "Unknown")
    return api_start_for("Unknown")


@app.post("/api/start_for/{user}")
def api_start_for(user: str):
    global last_start_user
    last_start_user = user

    # use last known amps from config; fallback to 16
    amps = latest_config.get("charge_amps") or 16
    payload = json.dumps({"charge_state": 1, "charge_amps": int(amps)})
    publish(payload)
    return {"ok": True, "amps": amps, "user": user}


@app.post("/api/stop")
def api_stop():
    publish(STOP_PAYLOAD)
    return {"ok": True}


@app.post("/api/amps/{amps}")
def api_amps(amps: int):
    payload = json.dumps({"charge_amps": amps})
    publish(payload)
    return {"ok": True, "amps": amps}


def _read_template(name: str) -> str:
    """Read an HTML template file from the templates directory."""
    template_path = TEMPLATES_DIR / name
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


def _check_auth(evse_auth: str | None):
    """Check if user is authenticated, return redirect if not."""
    if not _verify_auth_token(evse_auth):
        return RedirectResponse(url="/login", status_code=302)
    return None


@app.get("/login")
def login_page():
    html = _read_template("login.html")
    return HTMLResponse(html)


@app.post("/api/login")
def api_login(response: Response, pin: str):
    if pin == AUTH_PIN:
        token = _generate_auth_token()
        response.set_cookie(
            key=AUTH_COOKIE_NAME,
            value=token,
            max_age=AUTH_COOKIE_MAX_AGE,
            httponly=True,
            samesite="strict",
        )
        return {"ok": True}
    return {"ok": False, "error": "Invalid PIN"}


@app.get("/")
def ui(evse_auth: str | None = Cookie(default=None)):
    redirect = _check_auth(evse_auth)
    if redirect:
        return redirect
    html = _read_template("index.html")
    return HTMLResponse(html)


@app.get("/settings")
def settings_page(evse_auth: str | None = Cookie(default=None)):
    redirect = _check_auth(evse_auth)
    if redirect:
        return redirect
    html = _read_template("settings.html")
    return HTMLResponse(html)


@app.get("/sessions")
def sessions_page(evse_auth: str | None = Cookie(default=None)):
    redirect = _check_auth(evse_auth)
    if redirect:
        return redirect
    html = _read_template("sessions.html")
    return HTMLResponse(html)


@app.get("/calculator")
def calculator_page(evse_auth: str | None = Cookie(default=None)):
    redirect = _check_auth(evse_auth)
    if redirect:
        return redirect
    html = _read_template("calculator.html")
    return HTMLResponse(html)
