import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from paho.mqtt import client as mqtt

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

# ---- Templates directory ----
TEMPLATES_DIR = Path(__file__).parent / "templates"

def _load_settings() -> dict:
    defaults = {
        "clock_start": "07:00",
        "clock_end": "23:00",
        "users": ["Lidor", "Bar"],
        "price_per_kwh": 0.55,
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
                    "user": last_start_user or "Unknown",
                },
            }
            _save_sessions()
        elif current_session is not None and not is_active:
            # End of an existing session
            current_session["ended_at"] = ts
            current_session["end_amount_kwh"] = amount_val

            # Session energy purely from the amount counter
            start_amt = current_session.get("start_amount_kwh")
            if start_amt is not None and amount_val is not None:
                current_session["session_energy_kwh"] = max(
                    0.0, amount_val - start_amt
                )
            else:
                current_session["session_energy_kwh"] = None

            sessions.append(current_session)
            current_session = None
            _save_sessions()
        elif current_session is not None and is_active:
            # Update rolling values while charging
            current_session["end_amount_kwh"] = amount_val
            _save_sessions()


def on_connect(client, userdata, flags, rc):
    client.subscribe(f"{BASE}/availability")
    client.subscribe(f"{BASE}/state/charge")
    client.subscribe(f"{BASE}/state/config")


def on_message(client, userdata, msg):
    global latest_charge, latest_config, availability, last_mqtt_update
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


@app.get("/")
def ui():
    html = _read_template("index.html")
    return HTMLResponse(html)


@app.get("/settings")
def settings_page():
    html = _read_template("settings.html")
    return HTMLResponse(html)


@app.get("/sessions")
def sessions_page():
    html = _read_template("sessions.html")
    return HTMLResponse(html)
