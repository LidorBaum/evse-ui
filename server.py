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
    button { flex:1; padding:16px; font-size:18px; border-radius:12px; border:0; }
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
  <h2>BS20 Charger</h2>

  <div class="card">
    <div class="big">User</div>
    <select id="userSelect" style="margin-top:8px; padding:8px; border-radius:8px; width:100%; font-size:16px;">
      <option value="Lidor">Lidor</option>
      <option value="Bar">Bar</option>
    </select>
    <div class="muted" style="margin-top:6px;">Selected user will be attached to new sessions.</div>
  </div>

  <div class="card">
    <div class="row">
      <button class="start" onclick="startWithUser()">Start</button>
      <button class="stop" onclick="post('/api/stop')">Stop</button>
    </div>
  </div>
  <div class="card">
    <div class="big">Bluetooth Control</div>
    <div class="row" style="margin-top:8px;">
      <button onclick="pauseBle(30)">Pause 30s</button>
      <button onclick="pauseBle(60)">Pause 60s</button>
    </div>
    <div class="muted" id="pauseNote" style="margin-top:8px;"></div>
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
    <div class="big">History (last 10 sessions)</div>
    <div id="history" class="muted">No sessions yet.</div>
  </div>

<script>
let isDragging = false;
let debounceTimer = null;

// optimistic lock
let pendingAmps = null;
let lockUntilTs = 0;  // timestamp ms until which we don't overwrite slider

function currentUser(){
  const sel = document.getElementById('userSelect');
  return sel ? sel.value : 'Unknown';
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

async function pauseBle(sec){
  await post('/api/pause_ble/' + sec);
  document.getElementById('pauseNote').innerText =
    "BLE paused for " + sec + "s. You can connect with your phone app now.";
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

function fmtSession(s){
  const start = fmtDate(s.started_at);
  const ongoing = !s.ended_at;
  const end = ongoing ? '⚡ ongoing' : fmtDate(s.ended_at);

  // Compute energy: prefer session_energy_kwh, else delta, else live delta for ongoing
  let energy;
  if (s.session_energy_kwh != null) {
    energy = s.session_energy_kwh.toFixed(3) + ' kWh';
  } else if (s.end_amount_kwh != null && s.start_amount_kwh != null) {
    energy = (s.end_amount_kwh - s.start_amount_kwh).toFixed(3) + ' kWh';
  } else {
    energy = '0.000 kWh';
  }

  const user = (s.meta && s.meta.user) ? s.meta.user : 'Unknown';

  return `<div class="kv" style="margin-bottom:8px;">
    <div style="flex:1;">
      <div><b>${energy}</b> · ${user}${ongoing ? ' 🔌' : ''}</div>
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
    "Bridge: " + data.availability;

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

  document.getElementById('statusText').innerHTML =
    kv("Plug", ch.plug_state ?? '-') +
    kv("Output", ch.output_state ?? '-') +
    kv("State", ch.current_state ?? '-') +
    kv("Errors", ch.error_details ?? '-') ;

  const t = [];
  t.push(kv("L1 V", ch.l1_voltage ?? '-'));
  t.push(kv("L1 A", ch.l1_amperage ?? '-'));
  t.push(kv("L2 V", ch.l2_voltage ?? '-'));
  t.push(kv("L2 A", ch.l2_amperage ?? '-'));
  t.push(kv("L3 V", ch.l3_voltage ?? '-'));
  t.push(kv("L3 A", ch.l3_amperage ?? '-'));
  t.push(kv("Charging Rate", ch.total_energy != null ? ch.total_energy + ' kW' : '-'));
  t.push(kv("Inner °C", ch.inner_temp_c ?? '-'));
  t.push(kv("RSSI", cfg.rssi ?? '-'));
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

load();
loadSessions();
setInterval(load, 2000);
setInterval(loadSessions, 15000);
</script>

</body>
</html>
"""
    return HTMLResponse(html)
