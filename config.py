import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_API_ID = int(os.environ["TELEGRAM_API_ID"])
TELEGRAM_API_HASH = os.environ["TELEGRAM_API_HASH"]
NEWS_BOT_TOKEN = os.environ["NEWS_BOT_TOKEN"]
NEWS_CHAT_ID = int(os.environ["NEWS_CHAT_ID"])
LLM_API_KEY = os.environ["LLM_API_KEY"]

ALERT_BOT_TOKEN = os.environ["ALERT_BOT_TOKEN"]
ALERT_CHAT_ID = int(os.environ["ALERT_CHAT_ID"])
HEALTHCHECK_URL = os.environ["HEALTHCHECK_URL"]
