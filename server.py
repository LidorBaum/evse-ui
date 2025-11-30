import json
import os
import subprocess
import threading
import time
from datetime import datetime

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


@app.get("/")
def ui():
    html = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>BS20 Control</title>
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; margin: 16px; }
    .card { padding: 14px; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,.08); margin-bottom: 12px; }
    .row { display:flex; gap:10px; }
    button { flex:1; padding:16px; font-size:18px; border-radius:12px; border:0; cursor:pointer; }
    .start { background:#1db954; color:white; }
    .stop { background:#ff4d4f; color:white; }
    .muted { color:#666; font-size:14px; }
    .big { font-size:20px; font-weight:600; }
    input[type=range] { width:100%; }
    .grid { display:grid; grid-template-columns: 1fr 1fr; gap:8px; }
    .kv { display:flex; justify-content:space-between; }
  </style>
</head>
<body>
  <div style="display:flex; justify-content:space-between; align-items:center;">
    <h2 style="margin:0;">BS20 Charger</h2>
    <a href="/settings" style="font-size:24px; text-decoration:none;">⚙️</a>
  </div>

  <div class="card" style="margin-top:12px;">
    <div class="big">User</div>
    <select id="userSelect" style="margin-top:8px; padding:8px; border-radius:8px; width:100%; font-size:16px;">
    </select>
    <div class="muted" style="margin-top:6px;">Selected user will be attached to new sessions.</div>
  </div>

  <div class="card" id="controlCard">
    <div class="row" id="controlButtons">
      <!-- Buttons rendered by JS based on clock hours -->
    </div>
    <div class="muted" id="clockModeNote" style="margin-top:8px;"></div>
  </div>
  <div class="card">
    <div class="big">Max Amps: <span id="ampsVal">-</span>A</div>
    <input id="amps" type="range" min="6" max="16" step="1"
       oninput="ampsVal.innerText=this.value"
       onchange="setAmps()" />
  </div>

  <div class="card">
    <div class="big">Status</div>
    <div class="muted" id="availability">...</div>
    <div class="muted" id="lastUpdate" style="margin-top:4px;">Last update: -</div>
    <div id="statusText"></div>
  </div>

  <div class="card">
    <div class="big">Telemetry</div>
    <div class="grid" id="telemetry"></div>
  </div>

  <div class="card">
    <div style="display:flex; justify-content:space-between; align-items:center;">
      <div class="big">History</div>
      <a href="/sessions" style="font-size:14px;">View All →</a>
    </div>
    <div id="history" class="muted" style="margin-top:8px;">No sessions yet.</div>
  </div>

<script>
let isDragging = false;
let debounceTimer = null;

// optimistic lock
let pendingAmps = null;
let lockUntilTs = 0;  // timestamp ms until which we don't overwrite slider

let clockSettings = { clock_start: '07:00', clock_end: '23:00' };
let isCharging = false;
let hasData = false;  // true once we receive first MQTT data

function currentUser(){
  const sel = document.getElementById('userSelect');
  return sel ? sel.value : 'Unknown';
}

function isWithinClockHours(){
  // Get current time in Jerusalem
  const now = new Date();
  const jerusalemTime = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Jerusalem' }));
  const currentMinutes = jerusalemTime.getHours() * 60 + jerusalemTime.getMinutes();

  const [startH, startM] = clockSettings.clock_start.split(':').map(Number);
  const [endH, endM] = clockSettings.clock_end.split(':').map(Number);
  const startMinutes = startH * 60 + startM;
  const endMinutes = endH * 60 + endM;

  if (startMinutes <= endMinutes) {
    // Normal range (e.g. 07:00 - 23:00)
    return currentMinutes >= startMinutes && currentMinutes < endMinutes;
  } else {
    // Overnight range (e.g. 23:00 - 07:00)
    return currentMinutes >= startMinutes || currentMinutes < endMinutes;
  }
}

function renderControlButtons(){
  const container = document.getElementById('controlButtons');
  const note = document.getElementById('clockModeNote');
  const withinClock = isWithinClockHours();

  if (!hasData) {
    // No data yet – show disabled buttons
    container.innerHTML = `
      <button class="start" disabled style="opacity:0.5;cursor:not-allowed;">Start</button>
      <button class="stop" disabled style="opacity:0.5;cursor:not-allowed;">Stop</button>
    `;
    note.innerText = '⏳ Waiting for charger data...';
    return;
  }

  if (withinClock) {
    // Clock mode: single toggle button (Stop toggles both)
    const label = isCharging ? 'Stop ⏹️' : 'Start ▶️';
    const cls = isCharging ? 'stop' : 'start';
    container.innerHTML = `<button class="${cls}" onclick="post('/api/stop')" style="width:100%;">${label}</button>`;
    note.innerText = '🕐 Clock mode active – toggle charging with one button';
  } else {
    // Normal mode: separate Start/Stop
    container.innerHTML = `
      <button class="start" onclick="startWithUser()">Start</button>
      <button class="stop" onclick="post('/api/stop')">Stop</button>
    `;
    note.innerText = '';
  }
}

async function post(url){
  try{
    const r = await fetch(url, {method:'POST'});
    if(!r.ok) return false;
    return true;
  }catch(e){
    return false;
  }
}

async function startWithUser(){
  const user = encodeURIComponent(currentUser());
  await post('/api/start_for/' + user);
}

async function setAmps(){
  const v = Number(document.getElementById('amps').value);

  // lock UI to this value for 4 seconds
  pendingAmps = v;
  lockUntilTs = Date.now() + 4000;

  // send command
  await post('/api/amps/' + v);

  // after a bit, force a refresh
  setTimeout(load, 800);
}

function kv(k,v){
  return `<div class="kv"><div>${k}</div><div><b>${v}</b></div></div>`;
}

function fmtDate(isoStr){
  if (!isoStr) return '?';
  try {
    const d = new Date(isoStr);
    return d.toLocaleString('he-IL', {
      timeZone: 'Asia/Jerusalem',
      day: '2-digit',
      month: '2-digit',
      year: '2-digit',
      hour: '2-digit',
      minute: '2-digit'
    });
  } catch(e) {
    return isoStr;
  }
}

let pricePerKwh = 0;  // loaded from settings
const CLOCK_DISCOUNT = 0.8; // 20% off during clock hours

// Check if a given minute-of-day is within clock hours
function isMinuteInClock(minute) {
  const [startH, startM] = clockSettings.clock_start.split(':').map(Number);
  const [endH, endM] = clockSettings.clock_end.split(':').map(Number);
  const startMinutes = startH * 60 + startM;
  const endMinutes = endH * 60 + endM;

  if (startMinutes <= endMinutes) {
    return minute >= startMinutes && minute < endMinutes;
  } else {
    // Overnight range
    return minute >= startMinutes || minute < endMinutes;
  }
}

// Calculate minutes spent in clock vs non-clock for a session
function calcClockMinutes(startedAt, endedAt) {
  const startDate = new Date(startedAt);
  const endDate = endedAt ? new Date(endedAt) : new Date();

  let clockMins = 0;
  let nonClockMins = 0;

  // Iterate minute by minute (for accuracy across day boundaries)
  const current = new Date(startDate);
  while (current < endDate) {
    // Get Jerusalem time for this minute
    const jTime = new Date(current.toLocaleString('en-US', { timeZone: 'Asia/Jerusalem' }));
    const minuteOfDay = jTime.getHours() * 60 + jTime.getMinutes();

    if (isMinuteInClock(minuteOfDay)) {
      clockMins++;
    } else {
      nonClockMins++;
    }
    current.setMinutes(current.getMinutes() + 1);
  }

  return { clockMins, nonClockMins };
}

function calcSessionCost(energyVal, startedAt, endedAt) {
  if (energyVal <= 0) return 0;

  const { clockMins, nonClockMins } = calcClockMinutes(startedAt, endedAt);
  const totalMins = clockMins + nonClockMins;

  if (totalMins === 0) return energyVal * pricePerKwh;

  // Split energy proportionally by time
  const clockEnergy = energyVal * (clockMins / totalMins);
  const nonClockEnergy = energyVal * (nonClockMins / totalMins);

  // Clock hours get 20% discount
  const clockCost = clockEnergy * pricePerKwh * CLOCK_DISCOUNT;
  const nonClockCost = nonClockEnergy * pricePerKwh;

  return clockCost + nonClockCost;
}

function fmtSession(s){
  const start = fmtDate(s.started_at);
  const ongoing = !s.ended_at;
  const end = ongoing ? '⚡ ongoing' : fmtDate(s.ended_at);

  // Compute energy: prefer session_energy_kwh, else delta
  let energyVal = 0;
  if (s.session_energy_kwh != null) {
    energyVal = s.session_energy_kwh;
  } else if (s.end_amount_kwh != null && s.start_amount_kwh != null) {
    energyVal = s.end_amount_kwh - s.start_amount_kwh;
  }
  const energy = energyVal.toFixed(1) + ' kWh';
  const cost = '₪' + calcSessionCost(energyVal, s.started_at, s.ended_at).toFixed(2);

  const user = (s.meta && s.meta.user) ? s.meta.user : 'Unknown';

  return `<div class="kv" style="margin-bottom:8px;">
    <div style="flex:1;">
      <div><b>${energy}</b> · ${cost} · ${user}${ongoing ? ' 🔌' : ''}</div>
      <div style="font-size:12px;color:#888;">${start} → ${end}</div>
    </div>
  </div>`;
}

async function loadSessions(){
  try{
    const r = await fetch('/api/sessions');
    if (!r.ok) return;
    const data = await r.json();
    const list = data.sessions || [];
    const el = document.getElementById('history');
    if (!list.length){
      el.innerText = 'No sessions yet.';
      return;
    }
    const recent = list.slice(0, 10);
    el.innerHTML = recent.map(fmtSession).join('');
  }catch(e){
    // ignore
  }
}

let lastUpdateTs = 0;

function updateLastUpdateDisplay(){
  const el = document.getElementById('lastUpdate');
  if (!lastUpdateTs) {
    el.innerText = 'Last update: -';
    el.style.color = '#666';
    return;
  }
  const agoSec = Math.round((Date.now() / 1000) - lastUpdateTs);
  const stale = agoSec > 30;
  el.innerText = 'Last update: ' + agoSec + 's ago';
  el.style.color = stale ? '#ff4d4f' : '#1db954';
}

setInterval(updateLastUpdateDisplay, 1000);

async function load(){
  // don't fight user while dragging
  if (isDragging) return;

  const r = await fetch('/api/state');
  const data = await r.json();

  lastUpdateTs = data.last_update || 0;
  updateLastUpdateDisplay();

  document.getElementById('availability').innerText =
    "BLE Bridge: " + (data.availability === 'online' ? 'Connected ✅' : 'Disconnected ❌');

  const ch = data.charge || {};
  const cfg = data.config || {};

  const telemAmps = cfg.charge_amps;

  // ✅ Only overwrite slider from telemetry if:
  // - not dragging
  // - and either no pending amps, or telemetry already matched it, or lock expired
  const lockActive = pendingAmps !== null && Date.now() < lockUntilTs;
  const telemMatchesPending = pendingAmps !== null && telemAmps === pendingAmps;

  if (!lockActive || telemMatchesPending){
    if (telemMatchesPending) pendingAmps = null; // unlock once matched
    document.getElementById('amps').value = telemAmps ?? 16;
    document.getElementById('ampsVal').innerText = telemAmps ?? '-';
  } else {
    // keep showing optimistic value
    document.getElementById('amps').value = pendingAmps;
    document.getElementById('ampsVal').innerText = pendingAmps;
  }

  // Track charging state for toggle button
  const wasCharging = isCharging;
  const hadData = hasData;
  isCharging = (ch.output_state === 'Charging');
  hasData = Object.keys(ch).length > 0;  // We have data if charge object is not empty

  if (wasCharging !== isCharging || hadData !== hasData) {
    renderControlButtons();
  }

  document.getElementById('statusText').innerHTML =
    kv("Plug", ch.plug_state ?? '-') +
    kv("State", ch.output_state ?? '-') +
    kv("Errors", ch.error_details ?? '-') ;

  function fmtPhase(v, a) {
    if (v == null && a == null) return '-';
    const vStr = v != null ? Math.round(v) + 'V' : '-';
    const aStr = a != null ? Math.round(a) + 'A' : '-';
    return vStr + ' ' + aStr;
  }

  function rssiToWord(rssi) {
    if (rssi == null) return '-';
    if (rssi >= -50) return 'Excellent';
    if (rssi >= -70) return 'Good';
    if (rssi >= -80) return 'Fair';
    if (rssi >= -90) return 'Weak';
    return 'Very Weak';
  }

  const t = [];
  t.push(kv("L1", fmtPhase(ch.l1_voltage, ch.l1_amperage)));
  t.push(kv("L2", fmtPhase(ch.l2_voltage, ch.l2_amperage)));
  t.push(kv("L3", fmtPhase(ch.l3_voltage, ch.l3_amperage)));
  t.push(kv("Charging Rate", ch.total_energy != null ? ch.total_energy + ' kW' : '-'));
  t.push(kv("Inner °C", ch.inner_temp_c ?? '-'));
  t.push(kv("Signal Strength", rssiToWord(cfg.rssi)));
  document.getElementById('telemetry').innerHTML = t.join('');
}

// drag handlers
const ampsEl = document.getElementById('amps');

ampsEl.addEventListener('touchstart', () => { isDragging = true; });
ampsEl.addEventListener('mousedown',  () => { isDragging = true; });

function dragEnd(){
  isDragging = false;
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(setAmps, 400); // send shortly after release
}

ampsEl.addEventListener('touchend', dragEnd);
ampsEl.addEventListener('mouseup',  dragEnd);

// update number live while dragging
ampsEl.addEventListener('input', (e) => {
  document.getElementById('ampsVal').innerText = e.target.value;
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(setAmps, 600); // debounce during drag
});

async function loadSettings(){
  try {
    const r = await fetch('/api/settings');
    if (!r.ok) return;
    const settings = await r.json();

    // Update clock settings
    clockSettings.clock_start = settings.clock_start || '07:00';
    clockSettings.clock_end = settings.clock_end || '23:00';

    // Update price
    pricePerKwh = settings.price_per_kwh;

    // Update users dropdown
    const users = settings.users || ['Lidor', 'Bar'];
    const sel = document.getElementById('userSelect');
    sel.innerHTML = users.map(u => `<option value="${u}">${u}</option>`).join('');

    // Re-render control buttons based on clock
    renderControlButtons();
  } catch(e) {}
}

loadSettings();
load();
loadSessions();
renderControlButtons();
setInterval(load, 2000);
setInterval(loadSessions, 15000);
// Re-check clock hours every minute (in case time crosses boundary)
setInterval(renderControlButtons, 60000);
</script>

</body>
</html>
"""
    return HTMLResponse(html)


@app.get("/settings")
def settings_page():
    html = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>BS20 Settings</title>
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; margin: 16px; }
    .card { padding: 14px; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,.08); margin-bottom: 12px; }
    .muted { color:#666; font-size:14px; }
    .big { font-size:20px; font-weight:600; }
    input, select { padding:10px; border-radius:8px; border:1px solid #ccc; font-size:16px; width:100%; box-sizing:border-box; margin-top:8px; }
    button { padding:14px 24px; font-size:16px; border-radius:12px; border:1px solid #ccc; background:#fff; cursor:pointer; margin-top:12px; }
    button:hover { background:#f5f5f5; }
    .row { display:flex; gap:10px; align-items:center; }
  </style>
</head>
<body>
  <div style="display:flex; justify-content:space-between; align-items:center;">
    <h2 style="margin:0;">⚙️ Settings</h2>
    <a href="/" style="font-size:16px;">← Back</a>
  </div>

  <div class="card" style="margin-top:12px;">
    <div class="big">Clock Hours</div>
    <div class="muted">Charging schedule window (used by charger timer feature)</div>
    <div class="row" style="margin-top:12px;">
      <div style="flex:1;">
        <label class="muted">Start</label>
        <input type="time" id="clockStart" />
      </div>
      <div style="flex:1;">
        <label class="muted">End</label>
        <input type="time" id="clockEnd" />
      </div>
    </div>
  </div>

  <div class="card">
    <div class="big">Users</div>
    <div class="muted">Comma-separated list of user names for the dropdown</div>
    <input type="text" id="usersList" placeholder="Lidor, Bar" />
  </div>

  <div class="card">
    <div class="big">Price per kWh</div>
    <div class="muted">Electricity rate for cost estimation (₪)</div>
    <input type="number" id="pricePerKwh" step="0.01" min="0" />
  </div>

  <div class="card">
    <div class="big">Bluetooth Control</div>
    <div class="muted">Pause BLE bridge to connect with phone app</div>
    <button onclick="pauseBle(60)" style="margin-top:12px; width:100%;">Pause 60s</button>
    <div class="muted" id="pauseNote" style="margin-top:8px;"></div>
  </div>

  <button onclick="saveSettings()">Save Settings</button>
  <div id="status" class="muted" style="margin-top:12px;"></div>

<script>
async function loadSettings(){
  try {
    const r = await fetch('/api/settings');
    if (!r.ok) return;
    const s = await r.json();
    document.getElementById('clockStart').value = s.clock_start || '06:00';
    document.getElementById('clockEnd').value = s.clock_end || '23:00';
    document.getElementById('usersList').value = (s.users || []).join(', ');
    document.getElementById('pricePerKwh').value = s.price_per_kwh;
  } catch(e) {}
}

async function pauseBle(sec){
  try {
    const r = await fetch('/api/pause_ble/' + sec, {method: 'POST'});
    if (r.ok) {
      document.getElementById('pauseNote').innerText =
        "BLE paused for " + sec + "s. You can connect with your phone app now.";
    }
  } catch(e) {}
}

async function saveSettings(){
  const clockStart = document.getElementById('clockStart').value;
  const clockEnd = document.getElementById('clockEnd').value;
  const usersRaw = document.getElementById('usersList').value;
  const users = usersRaw.split(',').map(u => u.trim()).filter(u => u.length > 0);
  const pricePerKwh = parseFloat(document.getElementById('pricePerKwh').value) || 0;

  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        clock_start: clockStart,
        clock_end: clockEnd,
        users: users,
        price_per_kwh: pricePerKwh
      })
    });
    if (r.ok) {
      window.location.href = '/';
    } else {
      document.getElementById('status').innerText = '❌ Failed to save';
      document.getElementById('status').style.color = '#ff4d4f';
    }
  } catch(e) {
    document.getElementById('status').innerText = '❌ Error: ' + e.message;
    document.getElementById('status').style.color = '#ff4d4f';
  }
}

loadSettings();
</script>

</body>
</html>
"""
    return HTMLResponse(html)


@app.get("/sessions")
def sessions_page():
    html = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>BS20 Sessions</title>
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; margin: 16px; }
    .card { padding: 14px; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,.08); margin-bottom: 12px; }
    .muted { color:#666; font-size:14px; }
    .big { font-size:20px; font-weight:600; }
    .session { padding: 12px 0; border-bottom: 1px solid #eee; }
    .session:last-child { border-bottom: none; }
  </style>
</head>
<body>
  <div style="display:flex; justify-content:space-between; align-items:center;">
    <h2 style="margin:0;">📋 All Sessions</h2>
    <a href="/" style="font-size:16px;">← Back</a>
  </div>

  <div class="card" style="margin-top:12px;">
    <div id="sessionsList" class="muted">Loading...</div>
  </div>

<script>
let pricePerKwh = 0;  // loaded from settings
let clockSettings = { clock_start: '23:00', clock_end: '07:00' };
const CLOCK_DISCOUNT = 0.8;

function fmtDate(isoStr){
  if (!isoStr) return '?';
  try {
    const d = new Date(isoStr);
    return d.toLocaleString('he-IL', {
      timeZone: 'Asia/Jerusalem',
      day: '2-digit',
      month: '2-digit',
      year: '2-digit',
      hour: '2-digit',
      minute: '2-digit'
    });
  } catch(e) {
    return isoStr;
  }
}

function isMinuteInClock(minute) {
  const [startH, startM] = clockSettings.clock_start.split(':').map(Number);
  const [endH, endM] = clockSettings.clock_end.split(':').map(Number);
  const startMinutes = startH * 60 + startM;
  const endMinutes = endH * 60 + endM;

  if (startMinutes <= endMinutes) {
    return minute >= startMinutes && minute < endMinutes;
  } else {
    return minute >= startMinutes || minute < endMinutes;
  }
}

function calcClockMinutes(startedAt, endedAt) {
  const startDate = new Date(startedAt);
  const endDate = endedAt ? new Date(endedAt) : new Date();

  let clockMins = 0;
  let nonClockMins = 0;

  const current = new Date(startDate);
  while (current < endDate) {
    const jTime = new Date(current.toLocaleString('en-US', { timeZone: 'Asia/Jerusalem' }));
    const minuteOfDay = jTime.getHours() * 60 + jTime.getMinutes();

    if (isMinuteInClock(minuteOfDay)) {
      clockMins++;
    } else {
      nonClockMins++;
    }
    current.setMinutes(current.getMinutes() + 1);
  }

  return { clockMins, nonClockMins };
}

function calcSessionCost(energyVal, startedAt, endedAt) {
  if (energyVal <= 0) return 0;

  const { clockMins, nonClockMins } = calcClockMinutes(startedAt, endedAt);
  const totalMins = clockMins + nonClockMins;

  if (totalMins === 0) return energyVal * pricePerKwh;

  const clockEnergy = energyVal * (clockMins / totalMins);
  const nonClockEnergy = energyVal * (nonClockMins / totalMins);

  const clockCost = clockEnergy * pricePerKwh * CLOCK_DISCOUNT;
  const nonClockCost = nonClockEnergy * pricePerKwh;

  return clockCost + nonClockCost;
}

function fmtSession(s){
  const start = fmtDate(s.started_at);
  const ongoing = !s.ended_at;
  const end = ongoing ? '⚡ ongoing' : fmtDate(s.ended_at);

  let energyVal = 0;
  if (s.session_energy_kwh != null) {
    energyVal = s.session_energy_kwh;
  } else if (s.end_amount_kwh != null && s.start_amount_kwh != null) {
    energyVal = s.end_amount_kwh - s.start_amount_kwh;
  }
  const energy = energyVal.toFixed(1) + ' kWh';
  const cost = '₪' + calcSessionCost(energyVal, s.started_at, s.ended_at).toFixed(2);

  const user = (s.meta && s.meta.user) ? s.meta.user : 'Unknown';

  return `<div class="session">
    <div><b>${energy}</b> · ${cost} · ${user}${ongoing ? ' 🔌' : ''}</div>
    <div class="muted">${start} → ${end}</div>
  </div>`;
}

async function loadSettings(){
  try {
    const r = await fetch('/api/settings');
    if (!r.ok) return;
    const s = await r.json();
    pricePerKwh = s.price_per_kwh;
    clockSettings.clock_start = s.clock_start || '23:00';
    clockSettings.clock_end = s.clock_end || '07:00';
  } catch(e) {}
}

async function loadSessions(){
  try {
    const r = await fetch('/api/sessions');
    if (!r.ok) return;
    const data = await r.json();
    const list = data.sessions || [];
    const el = document.getElementById('sessionsList');
    if (!list.length) {
      el.innerText = 'No sessions yet.';
      return;
    }
    el.innerHTML = list.map(fmtSession).join('');
  } catch(e) {
    document.getElementById('sessionsList').innerText = 'Failed to load sessions.';
  }
}

loadSettings().then(() => loadSessions());
</script>

</body>
</html>
"""
    return HTMLResponse(html)
