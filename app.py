"""ELD webhook signals -> formatted Telegram alerts."""

import html
import json
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
SUBSCRIBERS_FILE = Path(os.getenv("SUBSCRIBERS_FILE", "subscribers.json"))
CHAT_ID = os.getenv("CHAT_ID", "").strip()  # eski konfiguratsiya uchun
CHAT_ID_1 = os.getenv("CHAT_ID_1", "").strip()
CHAT_ID_2 = os.getenv("CHAT_ID_2", "").strip()
PORT = int(os.getenv("PORT", "8000"))
MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", "65536"))
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
NA = "N/A"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("eld-alert-bot")
flask_app = Flask(__name__)
flask_app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
_subscriber_lock = threading.Lock()
_dedupe_lock = threading.Lock()
_recent_events: OrderedDict[str, float] = OrderedDict()
DEDUPE_SECONDS = int(os.getenv("DEDUPE_SECONDS", "300"))
TELEGRAM_CONNECT_TIMEOUT = float(os.getenv("TELEGRAM_CONNECT_TIMEOUT", "30.0"))
TELEGRAM_READ_TIMEOUT = float(os.getenv("TELEGRAM_READ_TIMEOUT", "30.0"))
TELEGRAM_BOOTSTRAP_RETRIES = int(os.getenv("TELEGRAM_BOOTSTRAP_RETRIES", "-1"))


def load_subscribers() -> set[int]:
    try:
        values = json.loads(SUBSCRIBERS_FILE.read_text(encoding="utf-8"))
        return {int(value) for value in values}
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return set()


def alert_recipients() -> set[int]:
    """Use fixed chat IDs when configured; otherwise use /start subscribers."""
    configured_ids = [value for value in (CHAT_ID_1, CHAT_ID_2) if value]
    if not configured_ids and CHAT_ID:
        configured_ids = [CHAT_ID]
    if configured_ids:
        recipients = set()
        for value in configured_ids:
            try:
                recipients.add(int(value))
            except ValueError:
                logger.error("CHAT_ID noto'g'ri: %r butun son bo'lishi kerak", value)
        return recipients
    return load_subscribers()


def save_subscribers(chat_ids: set[int]) -> None:
    SUBSCRIBERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = SUBSCRIBERS_FILE.with_suffix(SUBSCRIBERS_FILE.suffix + ".tmp")
    temporary_file.write_text(
        json.dumps(sorted(chat_ids), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary_file.replace(SUBSCRIBERS_FILE)


def add_subscriber(chat_id: int) -> bool:
    with _subscriber_lock:
        subscribers = load_subscribers()
        if chat_id in subscribers:
            return False
        subscribers.add(chat_id)
        save_subscribers(subscribers)
        return True


def remove_subscriber(chat_id: int) -> bool:
    with _subscriber_lock:
        subscribers = load_subscribers()
        if chat_id not in subscribers:
            return False
        subscribers.remove(chat_id)
        save_subscribers(subscribers)
        return True


def _field(data: dict, key: str) -> str:
    value = data.get(key)
    if value is None or not str(value).strip():
        return NA
    return html.escape(str(value).strip())


def _format_alert(data: dict, title: str, icon: str, note: str) -> str:
    return "\n".join(
        (
            f"{icon} <b>{title}</b>",
            f"🏢 Company: {_field(data, 'company_name')}",
            f"👤 Driver: {_field(data, 'driver_name')}",
            f"🚛 Truck Unit: {_field(data, 'truck_unit')}",
            f"📍 Location: {_field(data, 'location')}",
            f"⏰ Time: {_field(data, 'time')}",
            f"⚠️ Note: {note}",
        )
    )


def format_weigh_station(data: dict) -> str:
    return _format_alert(
        data,
        "WEIGH STATION NEARBY ALERT (20 Miles Left)",
        "⚖️",
        "Haydovchi tarozidan o'tishga tayyorlanishi va hujjatlar/logbook "
        "tartibda ekanligini tekshirishi kerak.",
    )


def format_log_frozen(data: dict) -> str:
    return _format_alert(
        data,
        "LOGBOOK FROZEN ALERT",
        "❄️",
        "Logbook holati muzlagan ko'rinadi. Iltimos, haydovchi bilan bog'laning "
        "va ilovani qayta yoqishini (restart) so'rang.",
    )


def format_driver_disconnected(data: dict) -> str:
    return _format_alert(
        data,
        "DRIVER DISCONNECTED ALERT",
        "🔌",
        "ELD qurilmasi bilan aloqa uzildi. Unidentified driving soatlarini oldini "
        "olish uchun zudlik bilan haydovchiga aloqaga chiqing.",
    )


SCENARIO_FORMATTERS = {
    "weigh_station": format_weigh_station,
    "weigh_station_20": format_weigh_station,
    "log_frozen": format_log_frozen,
    "logbook_frozen": format_log_frozen,
    "driver_disconnected": format_driver_disconnected,
    "eld_disconnected": format_driver_disconnected,
    "disconnected": format_driver_disconnected,
    "weighstation": format_weigh_station,
    "stationary_in_drive": format_log_frozen,
}

COMPANY_NAMES = {
    "7sky": "7SKY LOGISTICS INC",
    "msv": "MSV TRANSPORT LLC",
}

FIELD_ALIASES = {
    "scenario": ("scenario", "event", "event_type", "eventType", "alert_type", "alertType", "type"),
    "driver_name": ("driver_name", "driverName", "driver", "driver_full_name", "driverFullName"),
    "truck_unit": ("truck_unit", "truckUnit", "unit", "unit_number", "unitNumber", "vehicle"),
    "location": ("location", "address", "current_location", "currentLocation", "geo_location"),
    "time": ("time", "timestamp", "event_time", "eventTime", "occurred_at", "occurredAt"),
    "event_id": ("event_id", "eventId", "alert_id", "alertId", "id"),
}


def normalize_payload(payload: dict) -> dict:
    """Convert common provider field names and nested `data` payloads."""
    nested = payload.get("data")
    source = {**payload, **nested} if isinstance(nested, dict) else payload
    normalized = dict(source)
    for target, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            value = source.get(alias)
            if value is not None and str(value).strip():
                normalized[target] = value
                break
    scenario = str(normalized.get("scenario", "")).strip().lower()
    normalized["scenario"] = scenario.replace("-", "_").replace(" ", "_")
    return normalized


def is_duplicate(company_source: str | None, data: dict) -> bool:
    event_id = str(data.get("event_id", "")).strip()
    if not event_id:
        return False
    key = f"{company_source or 'legacy'}:{event_id}"
    now = time.monotonic()
    with _dedupe_lock:
        while _recent_events and next(iter(_recent_events.values())) < now - DEDUPE_SECONDS:
            _recent_events.popitem(last=False)
        if key in _recent_events:
            return True
        _recent_events[key] = now
        return False


def send_telegram_message(chat_id: int, message: str) -> bool:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN sozlanmagan")
        return False
    for attempt in range(3):
        try:
            response = requests.post(
                TELEGRAM_API_URL,
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
                timeout=(5, 15),
            )
            if response.ok:
                return True
            retry_after = 0
            if response.status_code == 429:
                try:
                    retry_after = min(float(response.json()["parameters"]["retry_after"]), 10)
                except (KeyError, TypeError, ValueError, requests.JSONDecodeError):
                    retry_after = 1
            if response.status_code not in (429, 500, 502, 503, 504):
                logger.error("Telegram xatosi (chat_id=%s): HTTP %s", chat_id, response.status_code)
                return False
            time.sleep(retry_after or attempt + 1)
        except requests.RequestException as exc:
            logger.warning("Telegram urinishi %s/3 bajarilmadi: %s", attempt + 1, exc)
            if attempt < 2:
                time.sleep(attempt + 1)
    return False


def broadcast_alert(message: str) -> dict[str, int]:
    subscribers = alert_recipients()
    sent = sum(send_telegram_message(chat_id, message) for chat_id in subscribers)
    return {
        "sent": sent,
        "failed": len(subscribers) - sent,
        "total_subscribers": len(subscribers),
    }


@flask_app.get("/health")
def health():
    subscribers = len(alert_recipients())
    ready = bool(BOT_TOKEN and subscribers)
    return jsonify({
        "status": "ok" if ready else "degraded",
        "bot_token_configured": bool(BOT_TOKEN),
        "webhook_protected": bool(WEBHOOK_SECRET),
        "subscribers": subscribers,
        "fixed_chat_configured": bool(CHAT_ID_1 or CHAT_ID_2 or CHAT_ID),
        "time": datetime.now(timezone.utc).isoformat(),
    }), 200 if ready else 503


def _handle_webhook(company_source: str | None = None):
    provided_secret = request.headers.get("X-Webhook-Secret", "")
    if WEBHOOK_SECRET and not secrets.compare_digest(provided_secret, WEBHOOK_SECRET):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object kerak"}), 400
    data = normalize_payload(data)

    # Company-specific endpoints are authoritative. A provider cannot accidentally
    # (or intentionally) put another carrier's name into the Telegram alert.
    if company_source:
        data = dict(data)
        data["company_name"] = COMPANY_NAMES[company_source]

    scenario = str(data.get("scenario", "")).strip().lower()
    formatter = SCENARIO_FORMATTERS.get(scenario)
    if formatter is None:
        return jsonify(
            {
                "error": "Noma'lum scenario",
                "allowed": ["weigh_station", "log_frozen", "driver_disconnected"],
            }
        ), 400

    if is_duplicate(company_source, data):
        return jsonify({
            "status": "duplicate_ignored",
            "company": COMPANY_NAMES.get(company_source, _field(data, "company_name")),
            "scenario": scenario,
        }), 200

    result = broadcast_alert(formatter(data))
    logger.info(
        "Alert: company=%s | scenario=%s | natija=%s",
        company_source or "legacy",
        scenario,
        result,
    )
    if result["total_subscribers"] == 0:
        delivery_status, status_code = "no_subscribers", 503
    elif result["sent"] == 0:
        delivery_status, status_code = "delivery_failed", 502
    elif result["failed"]:
        delivery_status, status_code = "partially_sent", 207
    else:
        delivery_status, status_code = "sent", 200
    return jsonify(
        {
            "status": delivery_status,
            "company": COMPANY_NAMES.get(company_source, _field(data, "company_name")),
            "scenario": scenario,
            **result,
        }
    ), status_code


@flask_app.post("/webhook/alert")
def webhook_alert():
    """Legacy/general endpoint; keeps the company_name supplied by the sender."""
    return _handle_webhook()


@flask_app.post("/webhook/7sky")
@flask_app.post("/webhook/7sky/alert")
def webhook_7sky():
    return _handle_webhook("7sky")


@flask_app.post("/webhook/msv")
@flask_app.post("/webhook/msv/alert")
def webhook_msv():
    return _handle_webhook("msv")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if add_subscriber(chat_id):
        await update.effective_message.reply_text(
            f"✅ Bu chat ELD ogohlantirishlari ro'yxatiga qo'shildi.\nChat ID: <code>{chat_id}</code>",
            parse_mode="HTML",
        )
    else:
        await update.effective_message.reply_text("ℹ️ Bu chat allaqachon ro'yxatda.")


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if remove_subscriber(update.effective_chat.id):
        await update.effective_message.reply_text("🛑 Bu chat ogohlantirishlar ro'yxatidan olib tashlandi.")
    else:
        await update.effective_message.reply_text("ℹ️ Bu chat ro'yxatda topilmadi.")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    subscribers = load_subscribers()
    subscribed = "Ha ✅" if update.effective_chat.id in subscribers else "Yo'q ❌"
    await update.effective_message.reply_text(
        f"📊 Faol obunachilar soni: {len(subscribers)}\nUshbu chat obunami: {subscribed}"
    )


def run_bot() -> None:
    # PTB uses a separate request object for long polling. Configuring both
    # prevents the initial getMe call and getUpdates loop from retaining the
    # short HTTPX defaults.
    api_request = HTTPXRequest(
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
        read_timeout=TELEGRAM_READ_TIMEOUT,
        write_timeout=TELEGRAM_READ_TIMEOUT,
        pool_timeout=TELEGRAM_CONNECT_TIMEOUT,
        connection_pool_size=8,
    )
    polling_request = HTTPXRequest(
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
        read_timeout=TELEGRAM_READ_TIMEOUT,
        write_timeout=TELEGRAM_READ_TIMEOUT,
        pool_timeout=TELEGRAM_CONNECT_TIMEOUT,
        connection_pool_size=2,
    )
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(api_request)
        .get_updates_request(polling_request)
        .build()
    )
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.run_polling(
        # Telegram long-poll duration stays below HTTPX's 30-second read limit.
        timeout=20,
        bootstrap_retries=TELEGRAM_BOOTSTRAP_RETRIES,
        poll_interval=1.0,
    )


if __name__ == "__main__":
    if BOT_TOKEN:
        flask_thread = threading.Thread(
            target=lambda: flask_app.run(host="0.0.0.0", port=PORT), daemon=True
        )
        flask_thread.start()
        logger.info("Webhook server 0.0.0.0:%s manzilida ishga tushdi", PORT)
        run_bot()
    else:
        logger.warning("BOT_TOKEN yo'q: faqat webhook server ishga tushadi")
        flask_app.run(host="0.0.0.0", port=PORT)
