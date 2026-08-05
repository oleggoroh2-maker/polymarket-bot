import asyncio
import logging
from html import escape
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
    get_active_subscriber_profiles,
    get_favorite_event,
    get_favorite_events,
    get_subscribers_count,
    get_subscriber_quality_mode,
    init_db,
    is_subscriber_active,
    update_favorite_note,
    set_subscriber_quality_mode,
)
from scanner import scan
from signal_engine import check_signals, format_alert
from opportunity_engine import check_opportunities, format_opportunity
from memory_engine import get_recent_memory_audit
from alert_formatter import format_calibrated_alert
from market_structure import enrich_market_structure
from similarity_engine import analyze_similarity
from cooldown_stats import (
    format_cooldown_dashboard,
    format_cooldown_summary,
    get_cooldown_dashboard,
)
from feature_engine import (
    format_feature_importance_report,
    get_feature_importance_report,
)
from adaptive_ai import (
    format_weight_proposal,
    generate_weight_proposal,
)
from ai_simulator import (
    format_simulator_report,
    get_simulator_report,
)
from confidence_engine import (
    format_confidence_report,
    get_confidence_report,
)
from price_intelligence import (
    format_price_intelligence_report,
    get_price_intelligence_report,
)
from feature_intelligence import (
    format_feature_intelligence_report,
    get_feature_intelligence_report,
)
from calibration_engine import (
    calibrate_signal,
    get_calibration_report,
    signal_passes_mode,
)


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
        ["🧠 Проверки AI", "🛡 Cooldown"],
        ["🧠 AI Insights", "🧠 Adaptive AI"],
        ["🧪 AI Simulator", "🎯 Confidence"],
        ["💰 Price Intelligence", "📊 Feature Intelligence"],
        ["⚙️ Качество сигналов"],
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

quality_keyboard = ReplyKeyboardMarkup(
    [
        ["🟢 Все сигналы", "🟡 Только хорошие"],
        ["⭐ Только Premium", "📊 Отчёт калибровки"],
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
    similarity = analyze_similarity(signal)
    calibrated = {**signal, **similarity}
    calibrated.update(calibrate_signal(calibrated))
    primary_change = calibrated.get("change")
    if primary_change is None:
        for key in ("change_5m", "change_15m", "change_1h", "change_24h"):
            if calibrated.get(key) is not None:
                primary_change = calibrated.get(key)
                break

    prepared = {
        **calibrated,
        "alert_label": "📊 MARKET SIGNAL",
        "alert_type": "MARKET_SIGNAL",
        "current_price": calibrated.get("price"),
        "change_percent": primary_change,
        "timeframe": "текущий скан",
    }
    return format_calibrated_alert(prepared, opportunity=False)


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
        "<b>⭐ МОЕ СОБЫТИЕ</b>",
        "",
        alert_text,
    ]

    note = (favorite.get("note") or "").strip()
    if note:
        lines.extend([
            "",
            "<b>📝 Моя заметка</b>",
            escape(note),
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
            parse_mode="HTML",
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
        parse_mode="HTML",
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
            parse_mode="HTML",
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
        cooldown_dashboard = await asyncio.to_thread(
            get_cooldown_dashboard,
            24,
            0,
        )

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
        f"✅ Сильное продолжение: {ai_stats['memory_24h']['successful']}\n"
        f"🟡 Частичное продолжение: {ai_stats['memory_24h']['partial']}\n"
        f"⚪ Без движения: {ai_stats['memory_24h']['neutral']}\n"
        f"❌ Против сигнала: {ai_stats['memory_24h']['failed']}\n"
        f"Сильное продолжение: "
        + (
            f"{ai_stats['memory_24h']['success_rate']:.1f}%\n"
            if ai_stats['memory_24h']['success_rate'] is not None
            else "накопление данных\n"
        )
        + "Любое продолжение: "
        + (
            f"{ai_stats['memory_24h']['continuation_rate']:.1f}%\n"
            if ai_stats['memory_24h']['continuation_rate'] is not None
            else "накопление данных\n"
        )
        + f"Средний результат: "
        + (
            f"{ai_stats['memory_24h']['average_directional_return']:+.1f}%\n"
            if ai_stats['memory_24h']['average_directional_return'] is not None
            else "накопление данных\n"
        )
        + f"Медиана: "
        + (
            f"{ai_stats['memory_24h']['median_directional_return']:+.1f}%\n"
            if ai_stats['memory_24h'].get('median_directional_return') is not None
            else "накопление данных\n"
        )
        + f"Обрезанное среднее (5%): "
        + (
            f"{ai_stats['memory_24h']['trimmed_mean_directional_return']:+.1f}%\n"
            if ai_stats['memory_24h'].get('trimmed_mean_directional_return') is not None
            else "накопление данных\n"
        )
        + f"Среднее |движение|: "
        + (
            f"{ai_stats['memory_24h']['mean_absolute_directional_return']:.1f}%\n"
            if ai_stats['memory_24h'].get('mean_absolute_directional_return') is not None
            else "накопление данных\n"
        )
        + f"Стандартное отклонение: "
        + (
            f"{ai_stats['memory_24h']['directional_return_stddev']:.1f}%\n"
            if ai_stats['memory_24h'].get('directional_return_stddev') is not None
            else "накопление данных\n"
        )
        + "\n🧮 Нормализованные метрики\n"
        + "Capped-среднее (±100%): "
        + (
            f"{ai_stats['memory_24h']['capped_average_directional_return']:+.1f}%\n"
            if ai_stats['memory_24h'].get('capped_average_directional_return') is not None
            else "накопление данных\n"
        )
        + "Signed-log среднее: "
        + (
            f"{ai_stats['memory_24h']['normalized_average_directional_return']:+.1f}%\n"
            if ai_stats['memory_24h'].get('normalized_average_directional_return') is not None
            else "накопление данных\n"
        )
        + "Signed-log медиана: "
        + (
            f"{ai_stats['memory_24h']['normalized_median_directional_return']:+.1f}%\n"
            if ai_stats['memory_24h'].get('normalized_median_directional_return') is not None
            else "накопление данных\n"
        )
        + "Signed-log σ: "
        + (
            f"{ai_stats['memory_24h']['normalized_directional_return_stddev']:.1f}%\n"
            if ai_stats['memory_24h'].get('normalized_directional_return_stddev') is not None
            else "накопление данных\n"
        )
        + "\n📈 Распределение результата\n"
        + f"≥ +50%: {ai_stats['memory_24h'].get('result_distribution', {}).get('gte_50', 0)}\n"
        + f"+20…+50%: {ai_stats['memory_24h'].get('result_distribution', {}).get('20_to_50', 0)}\n"
        + f"0…+20%: {ai_stats['memory_24h'].get('result_distribution', {}).get('0_to_20', 0)}\n"
        + f"0%: {ai_stats['memory_24h'].get('result_distribution', {}).get('zero', 0)}\n"
        + f"−20…0%: {ai_stats['memory_24h'].get('result_distribution', {}).get('minus_20_to_0', 0)}\n"
        + f"−50…−20%: {ai_stats['memory_24h'].get('result_distribution', {}).get('minus_50_to_minus_20', 0)}\n"
        + f"< −50%: {ai_stats['memory_24h'].get('result_distribution', {}).get('lt_minus_50', 0)}\n"
        + "\n💰 По стартовой цене\n"
        + "".join(
            f"• {item['label']}: Strong {item['strong_rate']:.1f}% · "
            f"Любое {item['continuation_rate']:.1f}% · "
            f"Норм. {item['normalized_average_return']:+.1f}% (n={item['samples']})\n"
            for item in ai_stats['memory_24h'].get('entry_price_buckets', [])
        )
        + f"PUMP: {ai_stats['memory_24h']['pump_successful']}/"
        f"{ai_stats['memory_24h']['pump_total']} сильных\n"
        f"DIP: {ai_stats['memory_24h']['dip_successful']}/"
        f"{ai_stats['memory_24h']['dip_total']} сильных\n\n"
        + format_cooldown_summary(cooldown_dashboard["stats"], compact=True)
    )


# ---------------- FEATURE IMPORTANCE / AI INSIGHTS ----------------

async def feature_insights_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text("🧠 Анализирую эффективность факторов...")
    try:
        report = await asyncio.to_thread(
            get_feature_importance_report,
            int(getattr(config, "FEATURE_IMPORTANCE_CHECKPOINT_MINUTES", 1440)),
            int(getattr(config, "FEATURE_IMPORTANCE_MAX_ROWS", 5000)),
            int(getattr(config, "FEATURE_IMPORTANCE_MIN_BUCKET_SAMPLES", 20)),
        )
    except Exception as error:
        logger.exception("Ошибка AI Insights")
        await update.message.reply_text(f"❌ Ошибка:\n{error}")
        return

    await update.message.reply_text(format_feature_importance_report(report))


# ---------------- ADAPTIVE AI / SHADOW WEIGHTS ----------------

async def adaptive_ai_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text("🧠 Рассчитываю теневые рекомендации весов...")
    try:
        proposal = await asyncio.to_thread(generate_weight_proposal)
    except Exception as error:
        logger.exception("Ошибка Adaptive AI")
        await update.message.reply_text(f"❌ Ошибка:\n{error}")
        return

    await update.message.reply_text(format_weight_proposal(proposal))


# ---------------- AI SIMULATOR / SHADOW BACKTEST ----------------

async def ai_simulator_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text("🧪 Сравниваю текущие и теневые веса...")
    try:
        report = await asyncio.to_thread(get_simulator_report)
    except Exception as error:
        logger.exception("Ошибка AI Simulator")
        await update.message.reply_text(f"❌ Ошибка:\n{error}")
        return

    await update.message.reply_text(format_simulator_report(report))


# ---------------- SIGNAL CONFIDENCE / SHADOW MODE ----------------

async def confidence_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text("🎯 Анализирую Confidence по проверенным сигналам...")
    try:
        report = await asyncio.to_thread(get_confidence_report)
    except Exception as error:
        logger.exception("Ошибка Signal Confidence")
        await update.message.reply_text(f"❌ Ошибка:\n{error}")
        return

    await update.message.reply_text(format_confidence_report(report))


# ---------------- PRICE INTELLIGENCE / SHADOW MODE ----------------

async def price_intelligence_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text("💰 Анализирую эффективность ценовых диапазонов...")
    try:
        report = await asyncio.to_thread(get_price_intelligence_report)
    except Exception as error:
        logger.exception("Ошибка Price Intelligence")
        await update.message.reply_text(f"❌ Ошибка:\n{error}")
        return

    await update.message.reply_text(format_price_intelligence_report(report))


# ---------------- FEATURE INTELLIGENCE / SHADOW MODE ----------------

async def feature_intelligence_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text("📊 Анализирую значимость и стабильность факторов...")
    try:
        report = await asyncio.to_thread(get_feature_intelligence_report)
    except Exception as error:
        logger.exception("Ошибка Feature Intelligence")
        await update.message.reply_text(f"❌ Ошибка:\n{error}")
        return

    await update.message.reply_text(format_feature_intelligence_report(report))


# ---------------- SMART COOLDOWN ANALYTICS ----------------

async def cooldown_stats_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text("🛡 Загружаю статистику Smart Cooldown...")
    try:
        dashboard = await asyncio.to_thread(
            get_cooldown_dashboard,
            24,
            10,
        )
    except Exception as error:
        logger.exception("Ошибка статистики Smart Cooldown")
        await update.message.reply_text(f"❌ Ошибка:\n{error}")
        return

    await update.message.reply_text(format_cooldown_dashboard(dashboard))


# ---------------- AI MEMORY AUDIT ----------------

def _audit_status_icon(status: str) -> str:
    return {
        "SUCCESS": "✅",
        "PARTIAL": "🟡",
        "NEUTRAL": "⚪",
        "FAIL": "❌",
    }.get(str(status).upper(), "⚪")


def _audit_direction_label(alert_type: str, alert_label: str) -> str:
    combined = f"{alert_type} {alert_label}".upper()
    if "DIP" in combined or "DROP" in combined or "BEAR" in combined:
        return "DIP"
    if "PUMP" in combined or "GROWTH" in combined or "BULL" in combined:
        return "PUMP"
    return alert_label or alert_type or "SIGNAL"


def format_memory_audit_item(item: dict[str, Any], number: int) -> str:
    entry_cents = float(item["entry_price"]) * 100.0
    measured_cents = float(item["measured_price"]) * 100.0
    status = str(item["status"]).upper()
    direction = _audit_direction_label(
        str(item.get("alert_type") or ""),
        str(item.get("alert_label") or ""),
    )

    return (
        f"{number}. {_audit_status_icon(status)} {item['title']}\n"
        f"Тип: {direction} | Статус: {status}\n"
        f"Сигнал: {str(item.get('created_at') or '')[:16].replace('T', ' ')} UTC\n"
        f"Проверка: {str(item.get('measured_at') or '')[:16].replace('T', ' ')} UTC\n"
        f"Цена: {entry_cents:.2f}¢ → {measured_cents:.2f}¢\n"
        f"Факт рынка: {float(item['return_percent']):+.1f}%\n"
        f"В направлении сигнала: "
        f"{float(item['directional_return_percent']):+.1f}%"
    )


async def memory_audit_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    await update.message.reply_text("🧠 Загружаю последние проверки...")

    try:
        items = await asyncio.to_thread(
            get_recent_memory_audit,
            1440,
            10,
        )
    except Exception as error:
        logger.exception("Ошибка аудита AI Memory")
        await update.message.reply_text(f"❌ Ошибка:\n{error}")
        return

    if not items:
        await update.message.reply_text(
            "🧠 Пока нет завершённых проверок за 24 часа."
        )
        return

    header = (
        "🧠 Последние проверки AI Memory (24ч)\n\n"
        "«Факт рынка» — обычное изменение цены.\n"
        "«В направлении сигнала» — результат с учётом PUMP/DIP.\n\n"
    )
    body = "\n\n".join(
        format_memory_audit_item(item, number)
        for number, item in enumerate(items, start=1)
    )

    await update.message.reply_text(header + body)


# ---------------- SIGNAL QUALITY ----------------

def _quality_mode_text(mode: str) -> str:
    return {
        "ALL": "🟢 Все сигналы",
        "GOOD": "🟡 Только хорошие",
        "PREMIUM": "⭐ Только Premium",
    }.get(str(mode).upper(), "🟢 Все сигналы")


async def quality_settings_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None or update.effective_chat is None:
        return
    mode = await asyncio.to_thread(
        get_subscriber_quality_mode,
        update.effective_chat.id,
    )
    await update.message.reply_text(
        "⚙️ Качество сигналов\n\n"
        f"Текущий режим: {_quality_mode_text(mode)}\n\n"
        "Все — максимальное количество алертов.\n"
        "Хорошие — GOOD и PREMIUM.\n"
        "Premium — только самые сильные по истории и AI.",
        reply_markup=quality_keyboard,
    )


async def set_quality_mode_action(
    update: Update,
    mode: str,
) -> None:
    if update.message is None or update.effective_chat is None:
        return
    await asyncio.to_thread(
        set_subscriber_quality_mode,
        update.effective_chat.id,
        mode,
    )
    await update.message.reply_text(
        f"✅ Режим установлен: {_quality_mode_text(mode)}",
        reply_markup=keyboard,
    )


async def calibration_report_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return
    report = await asyncio.to_thread(get_calibration_report)
    if not report.get("total"):
        await update.message.reply_text(
            "📊 Пока недостаточно завершённых проверок.",
            reply_markup=quality_keyboard,
        )
        return
    await update.message.reply_text(
        "📊 AI Calibration v1.0\n\n"
        f"Исторических проверок: {report['total']}\n"
        f"Сильное продолжение: {report['strong_rate']:.1f}%\n"
        f"Любое продолжение: {report['continuation_rate']:.1f}%\n"
        f"Средний результат: {report['average']:+.1f}%\n\n"
        f"🟢 GOOD: от {report['good_threshold']:.0f}/100\n"
        f"⭐ PREMIUM: от {report['premium_threshold']:.0f}/100\n\n"
        "Калибровка использует Score, AI Quality, AI Risk, ML, "
        "силу движения, объём, ликвидность и похожие исторические случаи.",
        reply_markup=quality_keyboard,
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
    set_subscriber_quality_mode,
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
        get_active_subscriber_profiles
    )

    if not subscribers:
        logger.info("Активных подписчиков нет")
        return

    for alert in alerts:
        alert = await asyncio.to_thread(enrich_market_structure, alert)
        if alert.get("alert_type") == "AI_OPPORTUNITY":
            alert_text = format_opportunity(alert)
        else:
            alert_text = format_alert(alert)

        market_id = get_market_id(alert)

        for subscriber in subscribers:
            chat_id = int(subscriber["chat_id"])
            quality_mode = str(subscriber.get("quality_mode") or "ALL")
            if not signal_passes_mode(alert, quality_mode):
                continue
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
                market_url = str(alert.get("url") or "").strip()
                reply_markup = None
                button_rows = []
                if market_url.startswith("http"):
                    button_rows.append([
                        InlineKeyboardButton("🌐 Polymarket", url=market_url),
                        InlineKeyboardButton("📊 График", url=market_url),
                    ])
                if market_id is not None:
                    alert_cache = context.application.bot_data.setdefault(
                        "alert_cache", {}
                    )
                    alert_cache[str(market_id)] = alert
                    # Keep the in-memory callback cache bounded.
                    if len(alert_cache) > 500:
                        for old_key in list(alert_cache)[:100]:
                            alert_cache.pop(old_key, None)
                    button_rows.append([
                        InlineKeyboardButton(
                            "⭐ В избранное",
                            callback_data=f"fav:{market_id}",
                        ),
                        InlineKeyboardButton(
                            "🔔 Следить",
                            callback_data="follow",
                        ),
                    ])
                if button_rows:
                    reply_markup = InlineKeyboardMarkup(button_rows)

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=personalized_text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
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


async def alert_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return

    data = str(query.data or "")

    if data == "follow":
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
        await query.answer("Уведомления включены ✅", show_alert=False)
        return

    if not data.startswith("fav:"):
        await query.answer()
        return

    market_id = data.split(":", 1)[1]
    cache = context.application.bot_data.get("alert_cache", {})
    alert = cache.get(market_id)
    if not isinstance(alert, dict):
        await query.answer(
            "Данные алерта устарели. Добавьте рынок через меню «Мои события».",
            show_alert=True,
        )
        return

    chat = update.effective_chat
    if chat is None:
        return


    await asyncio.to_thread(
        add_favorite_event,
        chat.id,
        market_id,
        str(alert.get("title") or "Polymarket event"),
        str(alert.get("url") or "") or None,
        None,
    )
    await query.answer("Добавлено в избранное ⭐", show_alert=False)


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

    elif text == "🧠 Проверки AI":
        await memory_audit_action(update, context)

    elif text == "🛡 Cooldown":
        await cooldown_stats_action(update, context)

    elif text == "🧠 AI Insights":
        await feature_insights_action(update, context)

    elif text == "🧠 Adaptive AI":
        await adaptive_ai_action(update, context)

    elif text == "🧪 AI Simulator":
        await ai_simulator_action(update, context)

    elif text == "🎯 Confidence":
        await confidence_action(update, context)

    elif text == "💰 Price Intelligence":
        await price_intelligence_action(update, context)

    elif text == "📊 Feature Intelligence":
        await feature_intelligence_action(update, context)

    elif text == "⚙️ Качество сигналов":
        await quality_settings_action(update, context)

    elif text == "🟢 Все сигналы":
        await set_quality_mode_action(update, "ALL")

    elif text == "🟡 Только хорошие":
        await set_quality_mode_action(update, "GOOD")

    elif text == "⭐ Только Premium":
        await set_quality_mode_action(update, "PREMIUM")

    elif text == "📊 Отчёт калибровки":
        await calibration_report_action(update, context)

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
            "🧠 Проверки AI — последние результаты AI Memory\n"
            "🛡 Cooldown — статистика и последние блокировки\n"
            "🧠 AI Insights — эффективность факторов и категорий\n"
            "🎯 Confidence — теневой итоговый рейтинг сигналов\n"
            "💰 Price Intelligence — эффективность стартовых цен\n"
            "📊 Feature Intelligence — значимость и стабильность факторов\n"
            "🧠 Adaptive AI — теневые рекомендации весов\n"
            "⚙️ Качество сигналов — фильтр ALL/GOOD/PREMIUM\n"
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
        CommandHandler(
            "memory_debug",
            memory_audit_action,
        )
    )

    application.add_handler(
        CommandHandler(
            "cooldown",
            cooldown_stats_action,
        )
    )

    application.add_handler(
        CommandHandler(
            "insights",
            feature_insights_action,
        )
    )

    application.add_handler(
        CommandHandler(
            "weights",
            adaptive_ai_action,
        )
    )

    application.add_handler(
        CommandHandler(
            "simulator",
            ai_simulator_action,
        )
    )

    application.add_handler(
        CommandHandler(
            "confidence",
            confidence_action,
        )
    )

    application.add_handler(
        CommandHandler(
            "price",
            price_intelligence_action,
        )
    )

    application.add_handler(
        CommandHandler(
            "features",
            feature_intelligence_action,
        )
    )

    application.add_handler(
        CallbackQueryHandler(alert_callback)
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