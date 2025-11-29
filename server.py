import subprocess
import threading
import json
import os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from paho.mqtt import client as mqtt

SERIAL = "0166341352572539"
BASE = f"evseMQTT/{SERIAL}"

# ---- Command topic + payloads ----
# evseMQTT doesn't document payloads clearly; so we make them configurable.
CMD_TOPIC = os.getenv("CMD_TOPIC", f"{BASE}/command")
START_PAYLOAD = '{"charge_state": 1}'
STOP_PAYLOAD  = '{"charge_state": 0}'
# Template: use {amps} placeholder
AMPS_PAYLOAD_TEMPLATE = '{"charge_amps": {amps}}'


MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

app = FastAPI()

latest_charge = {}
latest_config = {}
availability = "unknown"

def on_connect(client, userdata, flags, rc):
    client.subscribe(f"{BASE}/availability")
    client.subscribe(f"{BASE}/state/charge")
    client.subscribe(f"{BASE}/state/config")

def on_message(client, userdata, msg):
    global latest_charge, latest_config, availability
    topic = msg.topic
    payload = msg.payload.decode(errors="ignore")

    if topic.endswith("/availability"):
        availability = payload.strip()
    elif topic.endswith("/state/charge"):
        try:
            latest_charge = json.loads(payload)
        except:
            latest_charge = {"raw": payload}
    elif topic.endswith("/state/config"):
        try:
            latest_config = json.loads(payload)
        except:
            latest_config = {"raw": payload}

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
    }

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
    publish(START_PAYLOAD)
    return {"ok": True}

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
    <div class="row">
      <button class="start" onclick="post('/api/start')">Start</button>
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
    <div id="statusText"></div>
  </div>

  <div class="card">
    <div class="big">Telemetry</div>
    <div class="grid" id="telemetry"></div>
  </div>

<script>
let isDragging = false;
let debounceTimer = null;

// optimistic lock
let pendingAmps = null;
let lockUntilTs = 0;  // timestamp ms until which we don't overwrite slider

async function post(url){
  try{
    const r = await fetch(url, {method:'POST'});
    if(!r.ok) return false;
    return true;
  }catch(e){
    return false;
  }
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

async function load(){
  // don’t fight user while dragging
  if (isDragging) return;

  const r = await fetch('/api/state');
  const data = await r.json();

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
  t.push(kv("Session kWh", ch.current_energy ?? '-'));
  t.push(kv("Total kWh", ch.total_energy ?? '-'));
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
setInterval(load, 2000);
</script>

</body>
</html>
"""
    return HTMLResponse(html)
