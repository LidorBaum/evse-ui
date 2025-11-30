# EVSE-UI - Complete Setup Guide

> A comprehensive guide for setting up your Raspberry Pi and the EVSE-UI web application to control your EV charger via Bluetooth.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Raspberry Pi Setup](#raspberry-pi-setup)
3. [EVSE-UI Installation](#evse-ui-installation)
4. [Configuration](#configuration)
5. [Running the Application](#running-the-application)
6. [Using the App](#using-the-app)
7. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### Hardware

| Item | Description |
|------|-------------|
| **EV Charger** | Any charger compatible with the [EVSE Master](https://play.google.com/store/apps/details?id=com.evse.master) app (e.g., Besen BS20) |
| **Raspberry Pi** | Any model with built-in Bluetooth Low Energy (BLE) and WiFi (e.g., Pi 3B+, Pi 4, Pi Zero W/2W) |
| **Power Supply** | Appropriate power adapter for your Raspberry Pi |
| **MicroSD Card** | 8GB minimum, 16GB+ recommended |

### Network

- WiFi network that both your Raspberry Pi and phone/computer can connect to
- The Pi needs to be within Bluetooth range of your EV charger (~10 meters)

---

## Raspberry Pi Setup

### Step 1: Flash the Operating System

1. Download and install [Raspberry Pi Imager](https://www.raspberrypi.com/software/) on your computer
2. Insert your MicroSD card into your computer
3. Open Raspberry Pi Imager and select:
   - **Device**: Your Raspberry Pi model
   - **OS**: Raspberry Pi OS Lite (64-bit) — *found under "Raspberry Pi OS (other)"*
   - **Storage**: Your MicroSD card

### Step 2: Configure Settings (Important!)

Before clicking "Write", click the **gear icon** (⚙️) or **"Edit Settings"** to configure:

#### General Settings

| Setting | Value |
|---------|-------|
| **Hostname** | Choose a name (e.g., `evse-pi`) |
| **Username** | Create a username (e.g., `pi`) |
| **Password** | Set a strong password |
| **WiFi SSID** | Your WiFi network name |
| **WiFi Password** | Your WiFi password |
| **WiFi Country** | Your country code (e.g., `IL` for Israel, `US` for United States) |
| **Timezone** | Your timezone (e.g., `Asia/Jerusalem`) |

#### Services

| Setting | Value |
|---------|-------|
| **Enable SSH** | ✅ Enabled |
| **Authentication** | Password authentication |

> 💡 **Tip**: Write down your hostname, username, and password — you'll need them to connect via SSH.

Now click **"Write"** and wait for the process to complete.

### Step 3: First Boot

1. **Insert** the MicroSD card into your Raspberry Pi
2. **Place** the Pi within Bluetooth range of your EV charger (~10 meters)
3. **Power on** the Raspberry Pi
4. **Wait** 1-2 minutes for it to boot and connect to WiFi

### Step 4: Find Your Pi's IP Address

You can find the Pi's IP address using any of these methods:

| Method | How |
|--------|-----|
| **Router Admin** | Check your router's admin page for connected devices |
| **Network Scanner** | Use a phone app like "Fing" or "Network Scanner" |
| **Hostname** | If you set hostname to `evse-pi`, try: `ssh pi@evse-pi.local` |

### Step 5: Connect via SSH

Open a terminal (or PowerShell on Windows) and connect:

```bash
ssh your-username@your-pi-ip-address
```

For example:
```bash
ssh pi@192.168.1.100
```

Or using the hostname:
```bash
ssh pi@evse-pi.local
```

Enter your password when prompted. You're now connected to your Pi! 🎉

---

## EVSE-UI Installation

### Step 1: Update the System

Once connected via SSH, update your Pi:

```bash
sudo apt update && sudo apt upgrade -y
```

### Step 2: Install Required Packages

```bash
sudo apt install -y \
  git python3 python3-pip python3-venv \
  bluetooth bluez \
  mosquitto mosquitto-clients \
  curl
```

| Package | Purpose |
|---------|---------|
| `bluetooth` `bluez` | BLE stack for communicating with the charger |
| `mosquitto` | Local MQTT broker for commands & telemetry |
| `mosquitto-clients` | Terminal tools for MQTT debugging |
| `git` `python3` `pip` | Required for evseMQTT and evse-ui |
| `curl` | For installing Tailscale |

### Step 3: Enable MQTT Broker

```bash
sudo systemctl enable mosquitto
sudo systemctl start mosquitto
```

### Step 4: Install Tailscale (Remote Access)

Tailscale lets you access your Pi from anywhere, not just your home network.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

1. A login URL will appear — open it in your browser
2. Approve the device in your Tailscale account
3. Your Pi now has a Tailscale IP (like `100.x.y.z`)

> 💡 You can now SSH to your Pi from anywhere using the Tailscale IP!

### Step 5: Install evseMQTT

[evseMQTT](https://github.com/slespersen/evseMQTT) is the bridge between your charger's Bluetooth and MQTT.

```bash
cd ~
git clone https://github.com/slespersen/evseMQTT.git
cd evseMQTT
python3 -m pip install -r requirements.txt
```

### Step 6: Find Your Charger's Bluetooth MAC Address

1. **Put your charger in Bluetooth pairing mode** (refer to your charger's manual)

2. **Scan for Bluetooth devices**:

```bash
sudo bluetoothctl
```

Then inside bluetoothctl:

```
scan on
```

3. **Look for your charger** — it will appear with a MAC address like `AA:BB:CC:DD:EE:FF`

4. **Note down the MAC address** — you'll need it for configuration

5. **Exit bluetoothctl**:

```
scan off
exit
```

---

## Configuration

### Step 1: Configure evseMQTT

Navigate to the evseMQTT directory and edit the config file:

```bash
cd ~/evseMQTT
nano config.yaml
```

Fill in your details:

```yaml
charger:
  mac: "AA:BB:CC:DD:EE:FF"    # Your charger's MAC address from Step 6
  pin: "123456"                # Your charger's BLE PIN (same as in EVSE Master app)

mqtt:
  host: "localhost"
  port: 1883
```

Save and exit (`Ctrl+X`, then `Y`, then `Enter`).

### Step 2: Set Up evseMQTT as a Service

This ensures evseMQTT starts automatically on boot and reconnects if Bluetooth drops.

#### 2a) Create the Environment File

```bash
sudo nano /etc/default/evseMQTT
```

Paste the following (edit with your values):

```bash
BLE_ADDRESS=AA:BB:CC:DD:EE:FF
BLE_PASSWORD=YOUR_BLE_PIN
UNIT=C

MQTT_BROKER=localhost
MQTT_PORT=1883
MQTT_USER=
MQTT_PASSWORD=

LOGGING_LEVEL=INFO
```

| Variable | Description |
|----------|-------------|
| `BLE_ADDRESS` | Your charger's MAC address |
| `BLE_PASSWORD` | Your charger's BLE PIN (same as EVSE Master app) |
| `UNIT` | Temperature unit (`C` for Celsius) |

Save and exit (`Ctrl+X`, then `Y`, then `Enter`).

#### 2b) Create the Systemd Service

```bash
sudo nano /etc/systemd/system/evseMQTT.service
```

Paste the following (adjust the username and paths if different):

```ini
[Unit]
Description=evseMQTT BLE bridge for Besen BS20
After=network.target dbus.service mosquitto.service bluetooth.service
Wants=mosquitto.service bluetooth.service

[Service]
EnvironmentFile=/etc/default/evseMQTT
ExecStartPre=-/bin/systemctl start bluetooth
ExecStartPre=/bin/sleep 5
ExecStartPre=-/usr/bin/hciconfig hci0 up
ExecStartPre=/bin/sleep 2
ExecStart=/home/pi/evseMQTT/.venv/bin/evseMQTT \
  --address ${BLE_ADDRESS} \
  --password ${BLE_PASSWORD} \
  --unit ${UNIT} \
  --mqtt \
  --mqtt_broker ${MQTT_BROKER} \
  --mqtt_port ${MQTT_PORT} \
  --mqtt_user ${MQTT_USER} \
  --mqtt_password ${MQTT_PASSWORD} \
  --logging_level ${LOGGING_LEVEL} \
  --rssi
Restart=always
RestartSec=30
User=pi
WorkingDirectory=/home/pi/evseMQTT

[Install]
WantedBy=multi-user.target
```

> ⚠️ **Important**: Replace `pi` with your actual username if different.

The `ExecStartPre` commands ensure Bluetooth is properly initialized before evseMQTT starts:
- Starts the Bluetooth service
- Waits 5 seconds for it to initialize
- Brings up the Bluetooth adapter (`hci0`)
- Waits 2 more seconds before starting evseMQTT

Save and exit.

#### 2c) Enable and Start the Service

```bash
sudo systemctl daemon-reload
sudo systemctl enable evseMQTT
sudo systemctl start evseMQTT
sudo systemctl status evseMQTT --no-pager
```

You should see `active (running)` in green. ✅

> 💡 **Troubleshooting**: If the service fails, check logs with:
> ```bash
> journalctl -u evseMQTT -f
> ```

### Step 3: Verify Everything Works

#### Local Test

On your Pi, subscribe to MQTT messages:

```bash
mosquitto_sub -h localhost -t "evseMQTT/#" -v
```

You should see telemetry data flowing from your charger. Press `Ctrl+C` to stop.

#### Remote Test (via Tailscale)

1. On your phone, **turn off WiFi** (use cellular only)
2. SSH to your Pi using the **Tailscale IP** (e.g., `ssh pi@100.x.y.z`)
3. Run the same `mosquitto_sub` command

If you see data flowing, your remote access is working! 🎉

---

## Running the Application

### Step 1: Clone the evse-ui Repository

```bash
cd ~
git clone https://github.com/LidorBaum/evse-ui.git
cd evse-ui
```

### Step 2: Create a Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Step 3: Install Python Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### Step 4: Configure Environment Variables

Copy the example environment file and edit it:

```bash
cp env.example .env
nano .env
```

Fill in your settings:

```bash
# EVSE UI Configuration

# MQTT connection
MQTT_HOST=localhost
MQTT_PORT=1883

# Auth PIN (4 digits recommended)
AUTH_PIN=1234

# File paths (relative to server.py)
SESSIONS_FILE=sessions.json
SETTINGS_FILE=settings.json

# Max sessions to keep in history
MAX_SESSIONS=500

# Telegram notifications (optional)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Save and exit (`Ctrl+X`, then `Y`, then `Enter`).

#### Environment Variables Reference

| Variable | Description | Default |
|----------|-------------|---------|
| `MQTT_HOST` | MQTT broker hostname | `localhost` |
| `MQTT_PORT` | MQTT broker port | `1883` |
| `AUTH_PIN` | PIN code for web login | `1234` |
| `SESSIONS_FILE` | Path to sessions JSON file | `sessions.json` |
| `SETTINGS_FILE` | Path to settings JSON file | `settings.json` |
| `MAX_SESSIONS` | Maximum sessions to keep in history | `500` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (optional) | - |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID (optional) | - |

> 💡 **Telegram Setup** (optional):
> 1. Message [@BotFather](https://t.me/BotFather) on Telegram
> 2. Send `/newbot` and follow the prompts
> 3. Copy the bot token to `TELEGRAM_BOT_TOKEN`
> 4. Message [@userinfobot](https://t.me/userinfobot) to get your chat ID
> 5. Copy your chat ID to `TELEGRAM_CHAT_ID`

### Step 5: Test the Application

Run the server manually to make sure everything works:

```bash
.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8080
```

Open your browser and go to `http://YOUR_PI_IP:8080`

You should see the login page. Enter your PIN to access the dashboard.

Press `Ctrl+C` to stop the server.

### Step 6: Set Up evse-ui as a Service

Create the systemd service file:

```bash
sudo nano /etc/systemd/system/evse-ui.service
```

Paste the following (adjust username, paths, and serial number):

```ini
[Unit]
Description=EVSE-UI Web Dashboard
After=network.target mosquitto.service evseMQTT.service
Wants=mosquitto.service evseMQTT.service

[Service]
User=pi
WorkingDirectory=/home/pi/evse-ui
Environment=MQTT_HOST=localhost
Environment=MQTT_PORT=1883
Environment=CMD_TOPIC=evseMQTT/YOUR_CHARGER_SERIAL/command
Environment=START_PAYLOAD={"charge_state": 1}
Environment=STOP_PAYLOAD={"charge_state": 0}
Environment=AMPS_PAYLOAD_TEMPLATE={"charge_amps": {amps}}
ExecStart=/home/pi/evse-ui/.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> ⚠️ **Important**: 
> - Replace `pi` with your actual username
> - Replace `YOUR_CHARGER_SERIAL` with your charger's serial number (found in EVSE Master app or MQTT topics)

#### Environment Variables in Service File

| Variable | Description |
|----------|-------------|
| `MQTT_HOST` | MQTT broker hostname |
| `MQTT_PORT` | MQTT broker port |
| `CMD_TOPIC` | MQTT topic for sending commands to charger |
| `START_PAYLOAD` | JSON payload to start charging |
| `STOP_PAYLOAD` | JSON payload to stop charging |
| `AMPS_PAYLOAD_TEMPLATE` | Template for setting amperage (`{amps}` is replaced with the value)

Save and exit (`Ctrl+X`, then `Y`, then `Enter`).

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable evse-ui
sudo systemctl start evse-ui
sudo systemctl status evse-ui --no-pager
```

You should see `active (running)` in green. ✅

### Step 7: Access the Dashboard

Open your browser and navigate to:

| Access Method | URL |
|---------------|-----|
| **Local Network** | `http://YOUR_PI_IP:8080` |
| **Via Tailscale** | `http://YOUR_TAILSCALE_IP:8080` |
| **Via Hostname** | `http://evse-pi.local:8080` |

Log in with your PIN and start managing your charger! 🎉

---

## Using the App

### Dashboard Overview

The main dashboard shows:

| Section | Description |
|---------|-------------|
| **User** | Select who is charging (for session tracking) |
| **Controls** | Start/Stop charging buttons (or toggle in clock mode) |
| **Max Amps** | Slider to adjust charging amperage (6-16A) |
| **Status** | Connection status, plug state, charging state, errors |
| **Telemetry** | Real-time data: voltage, amperage, temperature, signal strength |
| **History** | Last 10 charging sessions with energy, cost, and duration |

### Features

#### 🔌 Charging Control
- **Start/Stop** - One-tap control with user attribution
- **Amps Adjustment** - Set charging current from 6A to 16A
- **Clock Mode** - Single toggle button during scheduled hours

#### 📊 Session Tracking
- Automatic session detection and logging
- Energy consumption (kWh) per session
- Cost calculation with off-peak discounts
- Battery percentage estimation
- Session notes for personal tracking

#### ⚡ Amps Calculator
Navigate to **Amps Calculator** to calculate optimal charging amps based on:
- Current battery percentage
- Target battery percentage
- Available charging time
- Cell balancing time

The calculator recommends the ideal amperage and can set it directly.

#### ⚙️ Settings
Configure your preferences:
- **Clock Hours** - Off-peak charging schedule
- **Clock Discount** - Percentage discount during clock hours
- **Users** - List of users for the dropdown
- **Price per kWh** - Electricity rate for cost calculation
- **Battery Capacity** - Your car's battery size (for calculations)
- **Bluetooth Control** - Pause BLE to connect with phone app
- **Telegram** - Test notifications
- **Export Data** - Send sessions/settings to Telegram

#### 📱 Telegram Notifications
Get notified when:
- ⚡ Charging starts
- ✅ Charging completes (with energy, cost, battery %)
- ⚠️ Charger errors occur

#### 🌙 Dark Mode
Toggle between light and dark themes (persists across sessions)

### Daily Backup

Sessions are automatically backed up to Telegram daily at 10:00 AM (if configured via cron).

---

## Troubleshooting

### Common Issues

#### evseMQTT won't connect to charger

1. **Check Bluetooth is enabled:**
   ```bash
   sudo systemctl status bluetooth
   sudo hciconfig hci0 up
   ```

2. **Verify charger is in pairing mode** (check your charger's manual)

3. **Check evseMQTT logs:**
   ```bash
   journalctl -u evseMQTT -f
   ```

4. **Verify MAC address and PIN** in `/etc/default/evseMQTT`

#### Web UI not loading

1. **Check evse-ui service:**
   ```bash
   sudo systemctl status evse-ui
   journalctl -u evse-ui -f
   ```

2. **Check if port 8080 is in use:**
   ```bash
   sudo lsof -i :8080
   ```

3. **Restart the service:**
   ```bash
   sudo systemctl restart evse-ui
   ```

#### No telemetry data showing

1. **Check MQTT broker is running:**
   ```bash
   sudo systemctl status mosquitto
   ```

2. **Test MQTT subscription:**
   ```bash
   mosquitto_sub -h localhost -t "evseMQTT/#" -v
   ```

3. **Check evseMQTT is connected:**
   ```bash
   sudo systemctl status evseMQTT
   ```

#### Can't access Pi remotely

1. **Check Tailscale status:**
   ```bash
   tailscale status
   ```

2. **Reconnect Tailscale:**
   ```bash
   sudo tailscale up
   ```

3. **Check Pi's IP:**
   ```bash
   tailscale ip -4
   ```

#### Bluetooth disconnects frequently

1. **Check signal strength** in the Telemetry section (should be "Good" or better)

2. **Move Pi closer** to the charger if possible

3. **Check for interference** from other Bluetooth devices

4. **Increase restart delay** in service file:
   ```bash
   sudo nano /etc/systemd/system/evseMQTT.service
   # Change RestartSec=30 to RestartSec=60
   sudo systemctl daemon-reload
   sudo systemctl restart evseMQTT
   ```

### Useful Commands

| Command | Description |
|---------|-------------|
| `sudo systemctl status evseMQTT` | Check evseMQTT status |
| `sudo systemctl status evse-ui` | Check evse-ui status |
| `sudo systemctl restart evseMQTT` | Restart evseMQTT |
| `sudo systemctl restart evse-ui` | Restart evse-ui |
| `journalctl -u evseMQTT -f` | View evseMQTT logs (live) |
| `journalctl -u evse-ui -f` | View evse-ui logs (live) |
| `mosquitto_sub -h localhost -t "evseMQTT/#" -v` | Monitor MQTT messages |
| `tailscale status` | Check Tailscale connection |
| `htop` | Monitor system resources |

### Getting Help

- **evseMQTT Issues**: [github.com/slespersen/evseMQTT](https://github.com/slespersen/evseMQTT/issues)
- **evse-ui Issues**: [github.com/LidorBaum/evse-ui](https://github.com/LidorBaum/evse-ui/issues)

---

## Quick Reference Card

```
┌─────────────────────────────────────────────────────────┐
│                    EVSE-UI Quick Reference              │
├─────────────────────────────────────────────────────────┤
│  Access UI:     http://<PI_IP>:8080                     │
│  Default PIN:   1234                                    │
├─────────────────────────────────────────────────────────┤
│  Services:                                              │
│    sudo systemctl [start|stop|restart] evseMQTT        │
│    sudo systemctl [start|stop|restart] evse-ui         │
├─────────────────────────────────────────────────────────┤
│  Logs:                                                  │
│    journalctl -u evseMQTT -f                           │
│    journalctl -u evse-ui -f                            │
├─────────────────────────────────────────────────────────┤
│  Config Files:                                          │
│    /etc/default/evseMQTT          (BLE settings)       │
│    ~/evse-ui/.env                 (UI settings)        │
│    ~/evse-ui/settings.json        (App settings)       │
│    ~/evse-ui/sessions.json        (Session history)    │
└─────────────────────────────────────────────────────────┘
```

---

*Guide last updated: December 2025*

---


