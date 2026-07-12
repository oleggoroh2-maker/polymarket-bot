import asyncio
import logging
from typing import Any, Optional

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from scanner import scan
from signal_engine import check_signals, format_alert


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
        ["🔔 Авто-режим", "ℹ Помощь"],
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
    ]

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


# ---------------- START ----------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    status = (
        "включён ✅"
        if AUTO_ALERTS
        else "выключен ❌"
    )

    await update.message.reply_text(
        "🤖 Polymarket Scanner запущен\n\n"
        f"Автоматический мониторинг: {status}\n"
        f"Интервал: {SCAN_INTERVAL // 60} мин.\n\n"
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
        f"🆕 Без истории: {new_markets}"
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

    for alert in alerts:
        try:
            await context.bot.send_message(
                chat_id=config.CHAT_ID,
                text=format_alert(alert),
                disable_web_page_preview=True,
            )

        except Exception:
            logger.exception(
                "Не удалось отправить алерт: %s",
                alert.get("title"),
            )


# ---------------- BUTTONS ----------------

async def handle_buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message is None:
        return

    text = update.message.text

    if text == "🔍 Сканировать":
        await scan_action(update, context)

    elif text == "⭐ Лучшая сделка":
        await best_action(update, context)

    elif text == "📊 ТОП-5":
        await top_action(update, context)

    elif text == "📈 Статистика":
        await stats_action(update, context)

    elif text == "🔔 Авто-режим":
        status = (
            "✅ Включён"
            if AUTO_ALERTS
            else "❌ Выключен"
        )

        await update.message.reply_text(
            "🔔 Автоматический мониторинг\n\n"
            f"Статус: {status}\n"
            f"Интервал: {SCAN_INTERVAL // 60} минут\n\n"
            "Автоматически отправляются только:\n"
            "🔴 STRONG DIP\n"
            "🚀 STRONG PUMP"
        )

    elif text == "ℹ Помощь":
        await update.message.reply_text(
            "🤖 Управление ботом\n\n"
            "🔍 Сканировать — ручной анализ\n"
            "⭐ Лучшая сделка — лучший рынок\n"
            "📊 ТОП-5 — пять лучших рынков\n"
            "📈 Статистика — сводка\n"
            "🔔 Авто-режим — статус мониторинга",
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

def main() -> None:
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