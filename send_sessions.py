#!/usr/bin/env python3
"""
Script to back up the EVSE data files via Telegram.
Can be run standalone or via cron.
Each file is sent only if it has changed since the last successful send.

Backed up: sessions.json, payments.json, settings.json.
(.env is intentionally NOT sent — it holds secrets/tokens.)

Usage:
    python send_sessions.py
    python send_sessions.py --force  # Send all, even if unchanged

Cron example (daily at 10:00 Jerusalem time):
    0 10 * * * cd /path/to/evse-ui && /path/to/venv/bin/python send_sessions.py
"""

import hashlib
import json
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
PAYMENTS_FILE = os.getenv("PAYMENTS_FILE", str(script_dir / "payments.json"))
SETTINGS_FILE = os.getenv("SETTINGS_FILE", str(script_dir / "settings.json"))

# (file_path, caption, hash_cache_file) — one cache per file so unchanged files skip.
BACKUP_FILES = [
    (SESSIONS_FILE, "📋 Daily sessions backup", script_dir / ".sessions_sent_hash"),
    (PAYMENTS_FILE, "💸 Daily payments backup", script_dir / ".payments_sent_hash"),
    (SETTINGS_FILE, "⚙️ Daily settings backup", script_dir / ".settings_sent_hash"),
]


def telegram_enabled() -> bool:
    """Read the saved preference on each send, including standalone cron runs."""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("telegram_enabled", True)
    except FileNotFoundError:
        return True
    except (OSError, ValueError, AttributeError) as e:
        print(f"Cannot read Telegram setting; skipping backups: {e}")
        return False


def get_file_hash(file_path: str) -> str:
    """Compute MD5 hash of a file's contents."""
    with open(file_path, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()


def get_cached_hash(cache_file: Path) -> str | None:
    """Read the cached hash from disk, or None if not exists."""
    if cache_file.exists():
        return cache_file.read_text().strip()
    return None


def save_cached_hash(cache_file: Path, file_hash: str) -> None:
    """Save the hash to disk."""
    cache_file.write_text(file_hash)


def has_file_changed(file_path: str, cache_file: Path) -> bool:
    """Check if the file has changed since last successful send."""
    if not os.path.exists(file_path):
        return False
    return get_file_hash(file_path) != get_cached_hash(cache_file)


def send_telegram_file(file_path: str, caption: str = "", silent: bool = True) -> tuple[bool, str]:
    """Send a file via Telegram bot."""
    if not telegram_enabled():
        return False, "Telegram messages are disabled in Settings"
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
        if not telegram_enabled():
            return False, "Telegram messages are disabled in Settings"
        urllib.request.urlopen(req, timeout=30)
        return True, "File sent successfully"
    except Exception as e:
        return False, str(e)


if __name__ == "__main__":
    import sys
    from datetime import datetime

    force_send = "--force" in sys.argv

    print(f"[{datetime.now().isoformat()}] Checking data-file backups...")

    if not telegram_enabled():
        print("Telegram messages are disabled; skipping backups.")
        sys.exit(0)

    any_failed = False
    for file_path, caption, cache_file in BACKUP_FILES:
        name = os.path.basename(file_path)

        if not os.path.exists(file_path):
            print(f"⏭️  {name}: not found, skipping.")
            continue

        if not force_send and not has_file_changed(file_path, cache_file):
            print(f"ℹ️  {name}: no changes, skipping.")
            continue

        print(f"📤 {name}: sending to Telegram...")
        success, message = send_telegram_file(file_path, caption=caption, silent=True)
        if success:
            save_cached_hash(cache_file, get_file_hash(file_path))
            print(f"✅ {name}: {message}")
        else:
            any_failed = True
            print(f"❌ {name}: {message}")

    if any_failed:
        exit(1)
