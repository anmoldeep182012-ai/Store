"""Environment-backed configuration."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")
MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "store_bot")
SESSION_ENCRYPTION_KEY = os.getenv("SESSION_ENCRYPTION_KEY", "")

TZ_OFFSET_MINUTES = int(os.getenv("TZ_OFFSET_MINUTES", "330"))
PAYMENT_TIMEOUT_MIN = int(os.getenv("PAYMENT_TIMEOUT_MIN", "30"))
MAX_PENDING_DEPOSITS = int(os.getenv("MAX_PENDING_DEPOSITS", "3"))
OTP_WINDOW_MIN = int(os.getenv("OTP_WINDOW_MIN", "5"))

DEFAULT_SETTINGS = {
    "store_name": "Account Store",
    "upi_id": os.getenv("UPI_ID", ""),
    "upi_name": os.getenv("UPI_NAME", "Store"),
    "support": os.getenv("SUPPORT", ""),
    "min_deposit": os.getenv("MIN_DEPOSIT", "50"),
    "ref_percent": os.getenv("REF_PERCENT", "5"),
    "maintenance": "0",
}
