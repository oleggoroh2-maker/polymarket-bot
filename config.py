import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
CHAT_ID_RAW = (os.getenv("CHAT_ID") or "").strip()

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

if not CHAT_ID_RAW:
    raise ValueError("CHAT_ID не задан")

CHAT_ID = int(CHAT_ID_RAW)

SCAN_INTERVAL = 300
AUTO_ALERTS = True

STRONG_DIP_PERCENT = -30
STRONG_PUMP_PERCENT = 30
MIN_ABSOLUTE_MOVE = 0.02
CHEAP_MARKET_MAX_PRICE = 0.01
CHEAP_MARKET_MIN_MOVE = 0.002

AUTO_VALUE_ALERTS = False
ALERT_COOLDOWN_HOURS = 24
