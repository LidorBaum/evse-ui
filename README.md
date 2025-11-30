# ⚡ MG4 Charger Dashboard

A beautiful, modern web dashboard for controlling and monitoring your Besen BS20 EV charger via Raspberry Pi.

![Dashboard Preview](https://img.shields.io/badge/Made%20for-MG4-00B140?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZmlsbD0id2hpdGUiIGQ9Ik0xOSA3aC0xVjZhMSAxIDAgMCAwLTEtMUg3YTEgMSAwIDAgMC0xIDF2MUg1YTMgMyAwIDAgMC0zIDN2OGEzIDMgMCAwIDAgMyAzaDE0YTMgMyAwIDAgMCAzLTNWMTBhMyAzIDAgMCAwLTMtM3ptMSAxMWExIDEgMCAwIDEtMSAxSDVhMSAxIDAgMCAxLTEtMXYtOGExIDEgMCAwIDEgMS0xaDE0YTEgMSAwIDAgMSAxIDF2OHoiLz48L3N2Zz4=)
![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![Tailwind CSS](https://img.shields.io/badge/Tailwind-38B2AC?style=for-the-badge&logo=tailwind-css&logoColor=white)

## ✨ Features

### 🎛️ Charger Control
- **Start/Stop Charging** - One-tap control with user attribution
- **Adjustable Amps** - Slider to set charging current (6-16A)
- **Clock Mode** - Smart toggle button during scheduled hours
- **Multi-User Support** - Track who's charging

### 📊 Real-time Monitoring
- **Live Telemetry** - Voltage, amperage, temperature, signal strength
- **Charging Status** - Plug state, output state, errors
- **BLE Bridge Status** - Connection health indicator

### 📈 Session Tracking
- **Automatic Logging** - Every charge session saved
- **Energy & Cost** - Track kWh and estimated cost (with clock hour discounts!)
- **Battery % Gain** - See how much battery you added
- **Session Notes** - Add custom notes to any session
- **Monthly Breakdown** - View stats by month
- **User Filtering** - Filter history by user

### 🔔 Notifications
- **Telegram Alerts** - Get notified on your phone for:
  - 🔌 Charging started
  - ⚡ Charging complete (with energy, cost, battery %)
  - ⚠️ Charger errors
- **Browser Notifications** - Desktop/mobile alerts
- **Error Banner** - Visual alert on dashboard

### 🧮 Amps Calculator
- Calculate optimal charging amps based on:
  - Current & target battery %
  - Available time
  - Balancing time
- One-tap to apply calculated amps

### 🌙 Dark Mode
- Automatic system preference detection
- Manual toggle
- Persists across sessions

### 🔐 Security
- PIN-based authentication
- Cookie-based sessions (7-day expiry)
- Protected API endpoints

## 🚀 Quick Start

### Prerequisites
- Raspberry Pi with Bluetooth
- Besen BS20 EV Charger
- [evseMQTT](https://github.com/example/evseMQTT) bridge running
- Python 3.10+

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/evse-ui.git
   cd evse-ui
   ```

2. **Create virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment**
   ```bash
   cp env.example .env
   nano .env  # Edit with your settings
   ```

5. **Run the server**
   ```bash
   uvicorn server:app --host 0.0.0.0 --port 8000
   ```

6. **Access the dashboard**
   Open `http://your-pi-ip:8000` in your browser

## ⚙️ Configuration

Edit `.env` file:

```bash
# MQTT connection (evseMQTT bridge)
MQTT_HOST=localhost
MQTT_PORT=1883

# Auth PIN (4 digits recommended)
AUTH_PIN=1234

# Telegram notifications (optional)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

### Settings (via UI)
- **Clock Hours** - Scheduled charging window (for timer & discount calculation)
- **Users** - List of users for the dropdown
- **Price per kWh** - Electricity rate for cost estimation
- **Battery Capacity** - Your car's battery size (for % calculations)

## 🔧 Running as a Service

Create `/etc/systemd/system/evse-ui.service`:

```ini
[Unit]
Description=EVSE UI Dashboard
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/evse-ui
Environment=PATH=/home/pi/evse-ui/venv/bin
ExecStart=/home/pi/evse-ui/venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable evse-ui
sudo systemctl start evse-ui
```

## 📱 Telegram Setup

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow prompts
3. Copy the bot token
4. Start a chat with your bot and send any message
5. Get your chat ID from: `https://api.telegram.org/bot<TOKEN>/getUpdates`
6. Add both to your `.env` file
7. Test with the "Send Test Message" button in Settings

## 🗂️ Project Structure

```
evse-ui/
├── server.py           # FastAPI backend
├── templates/          # HTML templates
│   ├── index.html      # Main dashboard
│   ├── settings.html   # Settings page
│   ├── sessions.html   # Session history
│   ├── calculator.html # Amps calculator
│   └── login.html      # Login page
├── sessions.json       # Session data (auto-created)
├── settings.json       # User settings (auto-created)
├── .env                # Environment config
├── env.example         # Example config
└── requirements.txt    # Python dependencies
```

## 📡 API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/state` | GET | Current charger state & telemetry |
| `/api/sessions` | GET | All charging sessions |
| `/api/settings` | GET/POST | App settings |
| `/api/start_for/{user}` | POST | Start charging |
| `/api/stop` | POST | Stop charging |
| `/api/amps/{value}` | POST | Set charging amps |
| `/api/session/{id}/note` | POST | Add note to session |
| `/api/telegram/test` | POST | Send test notification |
| `/api/pause_ble/{seconds}` | POST | Pause BLE bridge |

## 🎨 Tech Stack

- **Backend**: Python, FastAPI, Paho MQTT
- **Frontend**: HTML, Tailwind CSS, Vanilla JS
- **Data**: JSON file storage
- **Notifications**: Telegram Bot API, Web Notifications API

## 📄 License

MIT License - feel free to use and modify!

## 🙏 Acknowledgments

- [evseMQTT](https://github.com/example/evseMQTT) for the BLE-MQTT bridge
- [Tailwind CSS](https://tailwindcss.com) for the beautiful styling
- The MG4 community for inspiration

---

Made with ⚡ for EV enthusiasts

