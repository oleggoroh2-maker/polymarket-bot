import asyncio
import logging
from typing import Any, Optional

from telegram import ReplyKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from ai_engine import ensure_ai_schema, get_ai_stats
from database import (
    add_favorite_event,
    add_subscriber,
    cleanup_old_database_data,
    delete_favorite_event,
    disable_subscriber,
    get_active_subscribers,
    get_favorite_event,
    get_favorite_events,
    get_subscribers_count,
    init_db,
    is_subscriber_active,
    update_favorite_note,
)
from scanner import scan
from signal_engine import check_signals, format_alert
from opportunity_engine import check_opportunities, format_opportunity


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ---------------- SETTINGS ----------------

SCAN_INTERVAL = getattr(
    config,
    "SCAN_INTERVAL",
    300,
)

AUTO_ALERTS = getattr(
    config,
    "AUTO_ALERTS",
    True,
)


# ---------------- KEYBOARD ----------------

keyboard = ReplyKeyboardMarkup(
    [
        ["🔍 Сканировать", "⭐ Лучшая сделка"],
        ["📊 ТОП-5", "📈 Статистика"],
        ["⭐ Мои события"],
        ["🔔 Включить уведомления", "🔕 Отключить уведомления"],
        ["ℹ Помощь"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

favorites_keyboard = ReplyKeyboardMarkup(
    [
        ["➕ Добавить событие", "📋 Мои события"],
        ["✏️ Изменить заметку", "🗑 Удалить событие"],
        ["⬅️ Главное меню"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


# ---------------- HELPERS ----------------

def format_percent(
    value: Optional[float],
) -> str:
    if value is None:
        return "нет истории"

    return f"{value:+.1f}%"


def format_signal(
    signal: dict[str, Any],
) -> str:
    lines = [
        f"⭐ Score: {signal['score']}/100",
    ]

    ai_quality = signal.get("ai_quality")
    ai_risk = signal.get("ai_risk")
    ml_probability = signal.get("ml_probability")

    reasons = signal.get("opportunity_reasons") or []
    if reasons:
        lines.extend(["", "Почему рынок выделен:"])
        lines.extend(f"• {r}" for r in reasons)

    if ai_quality is not None:
        lines.append(f"🤖 AI Quality: {int(ai_quality)}/100")
    if ai_risk is not None:
        lines.append(f"⚠️ AI Risk: {int(ai_risk)}/100")
    if ml_probability is not None:
        lines.append(f"🧠 ML: {float(ml_probability) * 100:.1f}%")

    lines.extend([
        "",
        f"📊 {signal['title']}",
        "",
        f"💰 Цена: {signal['price'] * 100:.2f}¢",
        f"💧 Ликвидность: ${signal['liquidity']:,.0f}",
        f"📉 Momentum: {signal['momentum']}",
        f"🏷 {signal['category']}",
        f"⏳ {signal['days_left']} дней",
        "",
        f"5м: {format_percent(signal.get('change_5m'))}",
        f"15м: {format_percent(signal.get('change_15m'))}",
        f"1ч: {format_percent(signal.get('change_1h'))}",
        f"24ч: {format_percent(signal.get('change_24h'))}",
    ])

    url = signal.get("url")

    if url:
        lines.extend(
            [
                "",
                f"🌐 {url}",
            ]
        )

    return "\n".join(lines)


async def run_scan_in_thread() -> list[dict[str, Any]]:
    """
    Запускает синхронный scanner.scan() в отдельном потоке,
    чтобы Telegram-бот продолжал отвечать на кнопки.
    """
    return await asyncio.to_thread(scan)


async def subscribe_current_chat(update: Update) -> None:
    chat = update.effective_chat
    user = update.effective_user

    if chat is None:
        return

    await asyncio.to_thread(
        add_subscriber,
        chat.id,
        user.username if user else None,
        user.first_name if user else None,
    )


def get_market_id(item: dict[str, Any]) -> Optional[str]:
    for key in (
        "id",
        "market_id",
        "condition_id",
        "conditionId",
    ):
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()

    return None


def normalize_url(value: Optional[str]) -> str:
    if not value:
        return ""

    return str(value).strip().rstrip("/").lower()


def find_market_from_input(
    user_input: str,
    markets: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    query = user_input.strip()
    normalized_query = normalize_url(query)
    query_tail = normalized_query.rsplit("/", 1)[-1]

    for market in markets:
        market_id = get_market_id(market)
        market_url = normalize_url(market.get("url"))

        if market_id and query == market_id:
            return market

        if market_url and normalized_query == market_url:
            return market

        if market_url and query_tail:
            market_tail = market_url.rsplit("/", 1)[-1]
            if query_tail == market_tail:
                return market

        if market_id and market_id.lower() in normalized_query:
            return market

    return None


def format_favorite_alert(
    alert_text: str,
    favorite: dict[str, Optional[str]],
) -> str:
    lines = [
        "⭐ МОЕ СОБЫТИЕ",
        "",
        alert_text,
    ]

    note = (favorite.get("note") or "").strip()
    if note:
        lines.extend([
            "",
            "📝 Моя заметка",
            note,
        ])

    return "\n".join(lines)


def clear_favorite_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("favorite_state", None)
    context.user_data.pop("pending_favorite", None)
    context.user_data.pop("selected_favorite_id", None)


# ---------------- START ----------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await subscribe_current_chat(update)

    await update.message.reply_text(
        "🤖 Polymarket Scanner запущен\n\n"
        "🔔 Автоматические уведомления включены.\n"
        f"Интервал проверки: {SCAN_INTERVAL // 60} минут.\n\n"
        "Выберите действие 👇",
        reply_markup=keyboard,
    )


# ---------------- MANUAL SCAN ----------------

async def scan_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "🔍 Анализирую рынки..."
    )

    try:
        signals = await run_scan_in_thread()

    except Exception as error:
        logger.exception(
            "Ошибка ручного сканирования"
        )

        await update.message.reply_text(
            f"❌ Ошибка сканирования:\n{error}"
        )
        return

    if not signals:
        await update.message.reply_text(
            "❌ Подходящих рынков не найдено."
        )
        return

    count = min(5, len(signals))

    await update.message.reply_text(
        f"✅ Найдено рынков: {len(signals)}\n"
        f"Показываю первые {count}:"
    )

    for signal in signals[:count]:
        await update.message.reply_text(
            format_signal(signal),
            disable_web_page_preview=True,
        )


# ---------------- BEST SIGNAL ----------------

async def best_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "🏆 Ищу лучшую сделку..."
    )

    try:
        signals = await run_scan_in_thread()

    except Exception as error:
        logger.exception(
            "Ошибка поиска лучшей сделки"
        )

        await update.message.reply_text(
            f"❌ Ошибка:\n{error}"
        )
        return

    if not signals:
        await update.message.reply_text(
            "❌ Сигналы не найдены."
        )
        return

    await update.message.reply_text(
        "🏆 Лучшая сделка\n\n"
        + format_signal(signals[0]),
        disable_web_page_preview=True,
    )


# ---------------- TOP 5 ----------------

async def top_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "📊 Формирую ТОП-5..."
    )

    try:
        signals = await run_scan_in_thread()

    except Exception as error:
        logger.exception(
            "Ошибка получения ТОП-5"
        )

        await update.message.reply_text(
            f"❌ Ошибка:\n{error}"
        )
        return

    if not signals:
        await update.message.reply_text(
            "❌ Сигналы не найдены."
        )
        return

    for number, signal in enumerate(
        signals[:5],
        start=1,
    ):
        await update.message.reply_text(
            f"#{number}\n\n"
            f"{format_signal(signal)}",
            disable_web_page_preview=True,
        )


# ---------------- STATS ----------------

async def stats_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "📈 Собираю статистику..."
    )

    try:
        signals = await run_scan_in_thread()
        subscribers_count = await asyncio.to_thread(
            get_subscribers_count
        )
        ai_stats = await asyncio.to_thread(get_ai_stats)

    except Exception as error:
        logger.exception(
            "Ошибка получения статистики"
        )

        await update.message.reply_text(
            f"❌ Ошибка:\n{error}"
        )
        return

    if not signals:
        await update.message.reply_text(
            "Нет данных."
        )
        return

    average_score = (
        sum(
            signal["score"]
            for signal in signals
        )
        / len(signals)
    )

    dips = sum(
        1
        for signal in signals
        if "DIP" in signal["momentum"]
    )

    pumps = sum(
        1
        for signal in signals
        if (
            "PUMP" in signal["momentum"]
            or "GROWTH" in signal["momentum"]
        )
    )

    new_markets = sum(
        1
        for signal in signals
        if signal["momentum"] == "🆕 NEW"
    )

    await update.message.reply_text(
        "📊 Статистика\n\n"
        f"Просканировано рынков: {len(signals)}\n"
        f"Средний Score: {average_score:.1f}\n"
        f"📉 Падения: {dips}\n"
        f"🚀 Рост: {pumps}\n"
        f"🆕 Без истории: {new_markets}\n"
        f"🔔 Активных подписчиков: {subscribers_count}\n\n"
        "🤖 AI Core\n"
        f"Снимков рынка: {ai_stats['snapshots']}\n"
        f"Сигналов в базе: {ai_stats['signals']}\n"
        f"Контрольных замеров: {ai_stats['outcomes']}\n"
        f"Обучающих примеров: {ai_stats['training_samples']}/"
        f"{ai_stats['min_training_samples']}\n"
        f"ML-модель: {'готова ✅' if ai_stats['model_ready'] else 'накопление данных ⏳'}\n\n"
        "🧠 AI Memory (24ч)\n"
        f"Проверенных сигналов: {ai_stats['memory_24h']['total']}\n"
        f"Успешных: {ai_stats['memory_24h']['successful']}\n"
        f"Частичных: {ai_stats['memory_24h']['partial']}\n"
        f"Неудачных: {ai_stats['memory_24h']['failed']}\n"
        f"Точность: "
        + (
            f"{ai_stats['memory_24h']['success_rate']:.1f}%"
            if ai_stats['memory_24h']['success_rate'] is not None
            else "накопление данных"
        )
    )


# ---------------- FAVORITE EVENTS ----------------

async def favorites_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    clear_favorite_state(context)

    await update.message.reply_text(
        "⭐ Мои события\n\n"
        "Здесь можно добавить рынок Polymarket и оставить заметку.",
        reply_markup=favorites_keyboard,
    )


async def list_favorites_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None or update.effective_chat is None:
        return

    favorites = await asyncio.to_thread(
        get_favorite_events,
        update.effective_chat.id,
    )

    if not favorites:
        await update.message.reply_text(
            "⭐ Список пока пуст.",
            reply_markup=favorites_keyboard,
        )
        return

    lines = ["⭐ Мои события", ""]

    for number, favorite in enumerate(favorites, start=1):
        lines.append(f"{number}. {favorite['market_name']}")

        note = (favorite.get("note") or "").strip()
        if note:
            lines.append(f"📝 {note}")

        url = (favorite.get("url") or "").strip()
        if url:
            lines.append(f"🌐 {url}")

        lines.append("")

    await update.message.reply_text(
        "\n".join(lines).strip(),
        disable_web_page_preview=True,
        reply_markup=favorites_keyboard,
    )


async def begin_add_favorite(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    clear_favorite_state(context)
    context.user_data["favorite_state"] = "awaiting_market"

    await update.message.reply_text(
        "Отправьте ссылку Polymarket на событие или Market ID.\n\n"
        "Для отмены нажмите «⬅️ Главное меню».",
        reply_markup=favorites_keyboard,
    )


async def handle_favorite_market_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "🔍 Ищу событие среди рынков Polymarket..."
    )

    try:
        markets = await run_scan_in_thread()
    except Exception as error:
        logger.exception("Ошибка поиска избранного события")
        await update.message.reply_text(
            f"❌ Не удалось получить рынки:\n{error}",
            reply_markup=favorites_keyboard,
        )
        return

    market = find_market_from_input(text, markets)

    if market is None:
        await update.message.reply_text(
            "❌ Событие не найдено. Проверьте ссылку и отправьте её ещё раз.",
            reply_markup=favorites_keyboard,
        )
        return

    market_id = get_market_id(market)
    if market_id is None:
        await update.message.reply_text(
            "❌ У найденного события отсутствует Market ID.",
            reply_markup=favorites_keyboard,
        )
        return

    context.user_data["pending_favorite"] = {
        "market_id": market_id,
        "market_name": str(
            market.get("title")
            or market.get("question")
            or "Событие Polymarket"
        ),
        "url": market.get("url") or text.strip(),
    }
    context.user_data["favorite_state"] = "awaiting_note"

    await update.message.reply_text(
        "✅ Событие найдено:\n\n"
        f"{context.user_data['pending_favorite']['market_name']}\n\n"
        "Теперь отправьте заметку к событию.\n"
        "Например: «Жду цену ниже 8¢».\n\n"
        "Чтобы сохранить без заметки, отправьте знак -",
        reply_markup=favorites_keyboard,
    )


async def handle_favorite_note_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    if update.message is None or update.effective_chat is None:
        return

    pending = context.user_data.get("pending_favorite")
    if not isinstance(pending, dict):
        clear_favorite_state(context)
        await update.message.reply_text(
            "❌ Данные события потеряны. Добавьте его ещё раз.",
            reply_markup=favorites_keyboard,
        )
        return

    note = None if text.strip() == "-" else text.strip()

    await asyncio.to_thread(
        add_favorite_event,
        update.effective_chat.id,
        pending["market_id"],
        pending["market_name"],
        pending.get("url"),
        note,
    )

    clear_favorite_state(context)

    response = [
        "✅ Событие добавлено в «Мои события».",
        "",
        str(pending["market_name"]),
    ]
    if note:
        response.extend(["", f"📝 {note}"])

    await update.message.reply_text(
        "\n".join(response),
        reply_markup=favorites_keyboard,
    )


async def begin_delete_favorite(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None or update.effective_chat is None:
        return

    favorites = await asyncio.to_thread(
        get_favorite_events,
        update.effective_chat.id,
    )
    if not favorites:
        await update.message.reply_text(
            "⭐ Список пока пуст.",
            reply_markup=favorites_keyboard,
        )
        return

    context.user_data["favorite_state"] = "awaiting_delete_number"
    context.user_data["favorite_choices"] = favorites

    lines = ["Введите номер события, которое нужно удалить:", ""]
    lines.extend(
        f"{number}. {favorite['market_name']}"
        for number, favorite in enumerate(favorites, start=1)
    )

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=favorites_keyboard,
    )


async def handle_delete_number(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    if update.message is None or update.effective_chat is None:
        return

    choices = context.user_data.get("favorite_choices") or []

    try:
        index = int(text.strip()) - 1
        favorite = choices[index]
    except (ValueError, IndexError, TypeError):
        await update.message.reply_text(
            "❌ Отправьте корректный номер из списка.",
            reply_markup=favorites_keyboard,
        )
        return

    deleted = await asyncio.to_thread(
        delete_favorite_event,
        update.effective_chat.id,
        favorite["market_id"],
    )
    clear_favorite_state(context)
    context.user_data.pop("favorite_choices", None)

    await update.message.reply_text(
        (
            f"🗑 Событие удалено:\n{favorite['market_name']}"
            if deleted
            else "❌ Событие уже отсутствует в списке."
        ),
        reply_markup=favorites_keyboard,
    )


async def begin_edit_favorite_note(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None or update.effective_chat is None:
        return

    favorites = await asyncio.to_thread(
        get_favorite_events,
        update.effective_chat.id,
    )
    if not favorites:
        await update.message.reply_text(
            "⭐ Список пока пуст.",
            reply_markup=favorites_keyboard,
        )
        return

    context.user_data["favorite_state"] = "awaiting_edit_number"
    context.user_data["favorite_choices"] = favorites

    lines = ["Введите номер события для изменения заметки:", ""]
    lines.extend(
        f"{number}. {favorite['market_name']}"
        for number, favorite in enumerate(favorites, start=1)
    )

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=favorites_keyboard,
    )


async def handle_edit_number(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    if update.message is None:
        return

    choices = context.user_data.get("favorite_choices") or []

    try:
        index = int(text.strip()) - 1
        favorite = choices[index]
    except (ValueError, IndexError, TypeError):
        await update.message.reply_text(
            "❌ Отправьте корректный номер из списка.",
            reply_markup=favorites_keyboard,
        )
        return

    context.user_data["selected_favorite_id"] = favorite["market_id"]
    context.user_data["favorite_state"] = "awaiting_updated_note"

    await update.message.reply_text(
        f"Событие: {favorite['market_name']}\n\n"
        "Отправьте новую заметку. Чтобы удалить заметку, отправьте знак -",
        reply_markup=favorites_keyboard,
    )


async def handle_updated_note(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    if update.message is None or update.effective_chat is None:
        return

    market_id = context.user_data.get("selected_favorite_id")
    if not market_id:
        clear_favorite_state(context)
        await update.message.reply_text(
            "❌ Событие не выбрано.",
            reply_markup=favorites_keyboard,
        )
        return

    note = None if text.strip() == "-" else text.strip()
    updated = await asyncio.to_thread(
        update_favorite_note,
        update.effective_chat.id,
        str(market_id),
        note,
    )
    clear_favorite_state(context)
    context.user_data.pop("favorite_choices", None)

    await update.message.reply_text(
        "✅ Заметка обновлена." if updated else "❌ Событие не найдено.",
        reply_markup=favorites_keyboard,
    )


# ---------------- AUTO MONITOR ----------------

async def auto_scan_job(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Фоновая задача:
    scan() -> check_signals() -> Telegram.
    """

    if not AUTO_ALERTS:
        return

    logger.info(
        "Запущено автоматическое сканирование"
    )

    try:
        signals = await run_scan_in_thread()

        alerts = await asyncio.to_thread(
            check_signals,
            signals,
        )

        event_market_ids = {
            str(alert.get("id"))
            for alert in alerts
        }

        opportunities = await asyncio.to_thread(
            check_opportunities,
            signals,
        )

        alerts.extend(opportunities)

    except Exception:
        logger.exception(
            "Ошибка автоматического сканирования"
        )
        return

    if not alerts:
        logger.info(
            "Новых важных алертов нет"
        )
        return

    logger.info(
        "Обнаружено новых алертов: %s",
        len(alerts),
    )

    subscribers = await asyncio.to_thread(
        get_active_subscribers
    )

    if not subscribers:
        logger.info("Активных подписчиков нет")
        return

    for alert in alerts:
        if alert.get("alert_type") == "AI_OPPORTUNITY":
            alert_text = format_opportunity(alert)
        else:
            alert_text = format_alert(alert)

        market_id = get_market_id(alert)

        for chat_id in subscribers:
            personalized_text = alert_text

            if market_id is not None:
                favorite = await asyncio.to_thread(
                    get_favorite_event,
                    chat_id,
                    market_id,
                )

                if favorite is not None:
                    personalized_text = format_favorite_alert(
                        alert_text,
                        favorite,
                    )

            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=personalized_text,
                    disable_web_page_preview=True,
                )

            except Forbidden:
                logger.warning(
                    "Пользователь %s заблокировал бота",
                    chat_id,
                )
                await asyncio.to_thread(
                    disable_subscriber,
                    chat_id,
                )

            except BadRequest as error:
                logger.warning(
                    "Не удалось отправить сообщение %s: %s",
                    chat_id,
                    error,
                )

            except Exception:
                logger.exception(
                    "Ошибка отправки алерта подписчику %s",
                    chat_id,
                )


# ---------------- BUTTONS ----------------

async def handle_buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    text = update.message.text or ""

    if text == "⬅️ Главное меню":
        clear_favorite_state(context)
        context.user_data.pop("favorite_choices", None)
        await update.message.reply_text(
            "Главное меню 👇",
            reply_markup=keyboard,
        )
        return

    state = context.user_data.get("favorite_state")

    if state == "awaiting_market":
        await handle_favorite_market_input(update, context, text)
        return

    if state == "awaiting_note":
        await handle_favorite_note_input(update, context, text)
        return

    if state == "awaiting_delete_number":
        await handle_delete_number(update, context, text)
        return

    if state == "awaiting_edit_number":
        await handle_edit_number(update, context, text)
        return

    if state == "awaiting_updated_note":
        await handle_updated_note(update, context, text)
        return

    if text == "🔍 Сканировать":
        await scan_action(update, context)

    elif text == "⭐ Лучшая сделка":
        await best_action(update, context)

    elif text == "📊 ТОП-5":
        await top_action(update, context)

    elif text == "📈 Статистика":
        await stats_action(update, context)

    elif text == "⭐ Мои события":
        await favorites_action(update, context)

    elif text == "➕ Добавить событие":
        await begin_add_favorite(update, context)

    elif text == "📋 Мои события":
        await list_favorites_action(update, context)

    elif text == "🗑 Удалить событие":
        await begin_delete_favorite(update, context)

    elif text == "✏️ Изменить заметку":
        await begin_edit_favorite_note(update, context)

    elif text == "🔔 Включить уведомления":
        await subscribe_current_chat(update)
        await update.message.reply_text(
            "🔔 Автоматические уведомления включены."
        )

    elif text == "🔕 Отключить уведомления":
        chat = update.effective_chat

        if chat is None:
            return

        await asyncio.to_thread(
            disable_subscriber,
            chat.id,
        )

        await update.message.reply_text(
            "🔕 Автоматические уведомления отключены.\n"
            "Ручное сканирование продолжит работать."
        )

    elif text == "ℹ Помощь":
        chat = update.effective_chat
        active = False

        if chat is not None:
            active = await asyncio.to_thread(
                is_subscriber_active,
                chat.id,
            )

        status = "включены ✅" if active else "отключены ❌"

        await update.message.reply_text(
            "🤖 Управление ботом\n\n"
            "🔍 Сканировать — ручной анализ\n"
            "⭐ Лучшая сделка — лучший рынок\n"
            "📊 ТОП-5 — пять лучших рынков\n"
            "📈 Статистика — сводка\n"
            "⭐ Мои события — избранные рынки и заметки\n"
            "🔔 Включить уведомления — подписаться\n"
            "🔕 Отключить уведомления — отписаться\n\n"
            f"Ваши автоуведомления: {status}",
            reply_markup=keyboard,
        )

    else:
        await update.message.reply_text(
            "Выберите действие кнопками 👇",
            reply_markup=keyboard,
        )


# ---------------- ERROR HANDLER ----------------

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.exception(
        "Необработанная ошибка Telegram-бота",
        exc_info=context.error,
    )


# ---------------- MAIN ----------------
async def database_cleanup_job(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.info(
        "Запущена ежедневная очистка базы"
    )

    await asyncio.to_thread(
        cleanup_old_database_data,
        False,
    )


async def database_vacuum_job(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.info(
        "Запущено еженедельное обслуживание базы"
    )

    await asyncio.to_thread(
        cleanup_old_database_data,
        True,
    )

def main() -> None:
    init_db()
    ensure_ai_schema()

    if config.CHAT_ID is not None:
        add_subscriber(config.CHAT_ID)

    application = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_buttons,
        )
    )

    application.add_error_handler(
        error_handler
    )

    if AUTO_ALERTS:
        if application.job_queue is None:
            raise RuntimeError(
                "JobQueue недоступен. Выполни:\n"
                'pip install -U "python-telegram-bot[job-queue]"'
            )

        application.job_queue.run_repeating(
            callback=auto_scan_job,
            interval=SCAN_INTERVAL,
            first=10,
            name="polymarket_auto_scan",
        )

        application.job_queue.run_repeating(
            callback=database_cleanup_job,
            interval=int(
                getattr(
                    config,
                    "DATABASE_CLEANUP_INTERVAL_HOURS",
                    24,
                )
                * 60
                * 60
            ),
            first=15 * 60,
            name="database_daily_cleanup",
        )

        application.job_queue.run_repeating(
            callback=database_vacuum_job,
            interval=int(
                getattr(
                    config,
                    "DATABASE_VACUUM_INTERVAL_DAYS",
                    7,
                )
                * 24
                * 60
                * 60
            ),
            first=60 * 60,
            name="database_weekly_vacuum",
        )
    logger.info(
        "Polymarket Bot запущен"
    )

    logger.info(
        "Автоматический мониторинг: %s",
        "включён" if AUTO_ALERTS else "выключен",
    )

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()