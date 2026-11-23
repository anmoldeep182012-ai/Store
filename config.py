"""Configuration loaded from environment variables (.env)."""
import os

from dotenv import load_dotenv

load_dotenv()

def _int_list(value: str) -> list[int]:
    return [int(x) for x in value.replace(" ", "").split(",") if x]

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = set(_int_list(os.getenv("ADMIN_IDS", "")))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID") or 0) or None
CURRENCY = os.getenv("CURRENCY", "₹")
TZ_OFFSET_MINUTES = int(os.getenv("TZ_OFFSET_MINUTES", "330"))
PAYMENT_TIMEOUT_MIN = int(os.getenv("PAYMENT_TIMEOUT_MIN", "15"))
MAX_PENDING_DEPOSITS = int(os.getenv("MAX_PENDING_DEPOSITS", "3"))
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "15"))

# Server 2 (TG-Lion provider). Keep these secrets in .env, never in source control.
TG_LION_API_KEY = os.getenv("TG_LION_API_KEY", "")
TG_LION_YOUR_ID = os.getenv("TG_LION_YOUR_ID", "")
TG_LION_BASE_URL = os.getenv("TG_LION_BASE_URL", "https://tg-lion.net/")

API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH", "")
MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "store_bot")
SESSION_ENCRYPTION_KEY = os.getenv("SESSION_ENCRYPTION_KEY", "")

DEFAULT_SETTINGS = {
    "store_name": os.getenv("STORE_NAME", "Premium Account Store"),
    "upi_id": os.getenv("UPI_ID", ""),
    "upi_name": os.getenv("UPI_NAME", ""),
    "support": os.getenv("SUPPORT_CONTACT", ""),
    "min_deposit": os.getenv("MIN_DEPOSIT", "50"),
    "ref_percent": os.getenv("REFERRAL_PERCENT", "5"),
    "otp_hours": os.getenv("OTP_WINDOW_HOURS", "24"),
    "maintenance": "0",
    "server2_commission_inr": os.getenv("SERVER2_COMMISSION_INR", "20"),
    "server2_usd_inr": os.getenv("SERVER2_USD_INR", "90"),
}
