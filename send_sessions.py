#!/usr/bin/env python3
"""
Script to send sessions.json via Telegram.
Can be run standalone or via cron.

Usage:
    python send_sessions.py

Cron example (daily at 10:00 Jerusalem time):
    0 10 * * * cd /path/to/evse-ui && /path/to/venv/bin/python send_sessions.py
"""

import os
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the script's directory
script_dir = Path(__file__).parent
load_dotenv(script_dir / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SESSIONS_FILE = os.getenv("SESSIONS_FILE", str(script_dir / "sessions.json"))


def send_telegram_file(file_path: str, caption: str = "", silent: bool = True) -> tuple[bool, str]:
    """Send a file via Telegram bot."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False, "Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID)"
    
    if not os.path.exists(file_path):
        return False, f"File not found: {file_path}"
    
    try:
        # Read file content
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Create multipart form data
        boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
        filename = os.path.basename(file_path)
        
        body = (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
            f'{TELEGRAM_CHAT_ID}\r\n'
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
            f'Content-Type: application/json\r\n\r\n'
            f'{content}\r\n'
        )
        
        if caption:
            body += (
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="caption"\r\n\r\n'
                f'{caption}\r\n'
            )
        
        if silent:
            body += (
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="disable_notification"\r\n\r\n'
                f'true\r\n'
            )
        
        body += f'--{boundary}--\r\n'
        
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
        req = urllib.request.Request(
            url,
            data=body.encode('utf-8'),
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}
        )
        urllib.request.urlopen(req, timeout=30)
        return True, "File sent successfully"
    except Exception as e:
        return False, str(e)


if __name__ == "__main__":
    from datetime import datetime
    
    print(f"[{datetime.now().isoformat()}] Sending sessions backup to Telegram...")
    
    success, message = send_telegram_file(
        SESSIONS_FILE, 
        caption="📋 Daily sessions backup",
        silent=True
    )
    
    if success:
        print(f"✅ {message}")
    else:
        print(f"❌ {message}")
        exit(1)




