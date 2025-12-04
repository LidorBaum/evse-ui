#!/usr/bin/env python3
"""
Script to send sessions.json via Telegram.
Can be run standalone or via cron.
Only sends if the file has changed since the last successful send.

Usage:
    python send_sessions.py
    python send_sessions.py --force  # Send even if unchanged

Cron example (daily at 10:00 Jerusalem time):
    0 10 * * * cd /path/to/evse-ui && /path/to/venv/bin/python send_sessions.py
"""

import hashlib
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
HASH_CACHE_FILE = script_dir / ".sessions_sent_hash"


def get_file_hash(file_path: str) -> str:
    """Compute MD5 hash of a file's contents."""
    with open(file_path, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()


def get_cached_hash() -> str | None:
    """Read the cached hash from disk, or None if not exists."""
    if HASH_CACHE_FILE.exists():
        return HASH_CACHE_FILE.read_text().strip()
    return None


def save_cached_hash(file_hash: str) -> None:
    """Save the hash to disk."""
    HASH_CACHE_FILE.write_text(file_hash)


def has_file_changed(file_path: str) -> bool:
    """Check if the file has changed since last successful send."""
    if not os.path.exists(file_path):
        return False
    current_hash = get_file_hash(file_path)
    cached_hash = get_cached_hash()
    return current_hash != cached_hash


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
    import sys
    from datetime import datetime
    
    force_send = "--force" in sys.argv
    
    print(f"[{datetime.now().isoformat()}] Checking sessions backup...")
    
    # Check if file has changed
    if not force_send and not has_file_changed(SESSIONS_FILE):
        print("ℹ️  No changes detected, skipping send. Use --force to send anyway.")
        exit(0)
    
    print("📤 Changes detected, sending to Telegram..." if not force_send else "📤 Force sending to Telegram...")
    
    success, message = send_telegram_file(
        SESSIONS_FILE, 
        caption="📋 Daily sessions backup",
        silent=True
    )
    
    if success:
        # Update the cached hash after successful send
        save_cached_hash(get_file_hash(SESSIONS_FILE))
        print(f"✅ {message}")
    else:
        print(f"❌ {message}")
        exit(1)




