"""Compact HTML Telegram alerts with an expandable factual analysis block."""

from __future__ import annotations

from html import escape
from typing import Any, Optional

import config
from explain_engine import format_ai_explain


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _signed(value: float) -> str:
    return f"{value:+.1f}%"


def _money(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.0f}k"
    return f"${value:,.0f}"


def _cents(value: float) -> str:
    return f"{value * 100:.2f}¢"


def _direction(alert: dict[str, Any], opportunity: bool) -> str:
    text = " ".join(
        str(alert.get(key) or "").upper()
        for key in ("alert_type", "alert_label", "momentum")
    )
    change = _num(alert.get("change_percent"))
    if any(word in text for word in ("DIP", "DROP", "BEAR")) or change < 0:
        return "DIP"
    if any(word in text for word in ("PUMP", "GROWTH", "BULL")) or change > 0:
        return "PUMP"
    return "OPPORTUNITY" if opportunity else "NEUTRAL"


def _direction_label(direction: str) -> str:
    return {
        "PUMP": "📈 PUMP",
        "DIP": "📉 DIP",
        "OPPORTUNITY": "⭐ OPPORTUNITY",
        "NEUTRAL": "⚪ NEUTRAL",
    }.get(direction, "⚪ NEUTRAL")


def _risk_label(risk: float) -> str:
    if risk <= 30:
        return "низкий"
    if risk <= 55:
        return "средний"
    if risk <= 75:
        return "повышенный"
    return "высокий"


def _factor_icon(value: Optional[float]) -> str:
    if value is None:
        return "⚪"
    if value >= 10:
        return "🟢"
    if value >= 0:
        return "🟡"
    return "🔴"


def _ml_icon(ml: Optional[float]) -> str:
    if ml is None:
        return "⚪"
    percent = ml * 100.0
    if percent >= 70:
        return "🟢"
    if percent >= 40:
        return "🟡"
    return "🔴"


def _short_title(value: Any, limit: int = 110) -> str:
    title = str(value or "Без названия").strip()
    if len(title) <= limit:
        return title
    return title[: limit - 1].rstrip() + "…"


def _structure_lines(alert: dict[str, Any]) -> list[str]:
    rows: list[str] = ["<b>📊 СТРУКТУРА РЫНКА</b>"]
    yes_price = _optional_num(alert.get("yes_price"))
    no_price = _optional_num(alert.get("no_price"))
    best_bid = _optional_num(alert.get("best_bid"))
    best_ask = _optional_num(alert.get("best_ask"))
    spread = _optional_num(alert.get("spread"))
    bid_depth = _optional_num(alert.get("bid_depth"))
    ask_depth = _optional_num(alert.get("ask_depth"))
    bid_balance = _optional_num(alert.get("bid_balance"))
    largest = _optional_num(alert.get("largest_order"))

    if yes_price is not None:
        rows.append(f"YES: {_cents(yes_price)}")
    if no_price is not None:
        rows.append(f"NO: {_cents(no_price)}")
    if best_bid is not None:
        rows.append(f"Best bid: {_cents(best_bid)}")
    if best_ask is not None:
        rows.append(f"Best ask: {_cents(best_ask)}")
    if spread is not None:
        rows.append(f"Spread: {_cents(spread)}")
    if bid_depth is not None:
        rows.append(f"Глубина bid: {_money(bid_depth)}")
    if ask_depth is not None:
        rows.append(f"Глубина ask: {_money(ask_depth)}")
    if bid_balance is not None:
        rows.append(f"Баланс стакана: {bid_balance:.0f}% / {100.0 - bid_balance:.0f}%")
    if largest is not None:
        rows.append(f"Крупнейшая заявка: {_money(largest)}")

    if len(rows) == 1:
        return []
    return rows


def _plain_explain(alert: dict[str, Any]) -> str:
    text = format_ai_explain(
        alert,
        max_factors=int(getattr(config, "AI_EXPLAIN_MAX_FACTORS", 8)),
    )
    # The existing explain formatter is plain text; escape it before HTML embedding.
    return escape(text)


def format_calibrated_alert(
    alert: dict[str, Any],
    *,
    opportunity: bool = False,
) -> str:
    direction = _direction(alert, opportunity)
    badge = escape(str(alert.get("calibration_badge") or "🟡 WATCH"))
    confidence = _num(alert.get("calibration_confidence"))
    score = int(_num(alert.get("score")))
    quality = int(_num(alert.get("ai_quality")))
    risk = int(_num(alert.get("ai_risk")))
    current_price = _num(alert.get("current_price", alert.get("price")))
    old_price = _optional_num(alert.get("old_price"))
    change = _optional_num(alert.get("change_percent"))
    volume_change = _optional_num(alert.get("volume_change_percent"))
    liquidity_change = _optional_num(alert.get("liquidity_change_percent"))
    ml = _optional_num(alert.get("ml_probability"))
    liquidity = _num(alert.get("liquidity"))
    samples = int(_num(alert.get("calibration_samples")))
    strong_rate = _optional_num(alert.get("calibration_strong_rate"))
    timeframe = escape(str(alert.get("timeframe") or "—"))
    momentum = escape(str(alert.get("momentum") or "—"))
    title = escape(_short_title(alert.get("title")))

    header = [
        f"<b>{badge}</b>   <b>{_direction_label(direction)}</b>",
        f"<b>{title}</b>",
        "",
    ]
    if old_price is not None:
        price_line = f"💰 {_cents(old_price)} → {_cents(current_price)}"
    else:
        price_line = f"💰 {_cents(current_price)}"
    if change is not None:
        price_line += f"  (<b>{_signed(change)}</b>)"
    header.append(price_line)
    header.append(f"⏱ {timeframe}  |  💧 {_money(liquidity)}")
    header.append("")

    if change is not None:
        header.append(f"🟢 Цена {_signed(change)}")
    if volume_change is not None:
        header.append(f"{_factor_icon(volume_change)} Объём {_signed(volume_change)}")
    if liquidity_change is not None:
        header.append(f"{_factor_icon(liquidity_change)} Ликвидность {_signed(liquidity_change)}")
    if ml is not None:
        header.append(f"{_ml_icon(ml)} ML {ml * 100:.1f}%")

    if samples and strong_rate is not None:
        header.extend(["", f"🏆 История: <b>{strong_rate:.1f}%</b> из {samples} случаев"])

    # Short conclusion from existing explain engine: use only text after its conclusion heading.
    full_explain = format_ai_explain(alert, max_factors=8)
    conclusion = full_explain.split("💡 Вывод AI", 1)[-1].strip()
    if conclusion:
        compact = " ".join(conclusion.split())
        if len(compact) > 240:
            compact = compact[:237].rstrip() + "…"
        header.extend(["", f"💡 {escape(compact)}"])

    details = [
        "<b>🧠 AI АНАЛИЗ</b>",
        f"Score: {score}/100",
        f"AI Quality: {quality}/100",
        f"AI Risk: {risk}/100 ({_risk_label(risk)})",
        f"ML: {ml * 100:.1f}%" if ml is not None else "ML: накопление данных",
        "",
        "<b>🎯 ПРОГНОЗ AI</b>",
        f"Направление: {direction}",
        f"Изменение: {_signed(change)}" if change is not None else "Изменение: —",
        f"Калибровка: {confidence:.0f}/100",
        "",
        "<b>🚀 МОМЕНТУМ</b>",
        f"Momentum: {momentum}",
        f"Объём: {_signed(volume_change)}" if volume_change is not None else "Объём: нет истории",
        f"Ликвидность: {_signed(liquidity_change)}" if liquidity_change is not None else "Ликвидность: нет истории",
        "",
        "<b>🛡 БЕЗОПАСНОСТЬ</b>",
        f"AI Risk: {_risk_label(risk)} ({risk}/100)",
        f"Текущая ликвидность: {_money(liquidity)}",
    ]

    structure = _structure_lines(alert)
    if structure:
        details.extend(["", *structure])

    details.extend([
        "",
        "<b>⚖️ КАЛИБРОВКА</b>",
        f"Rating: {confidence:.0f}/100",
        f"Качество: {badge}",
    ])
    if samples and strong_rate is not None:
        details.extend([
            f"Похожих случаев: {samples}",
            f"Сильное продолжение: {strong_rate:.1f}%",
        ])

    details.extend([
        "",
        "<b>📅 ИНФОРМАЦИЯ</b>",
        f"Категория: {escape(str(alert.get('category') or '—'))}",
        f"До завершения: {int(_num(alert.get('days_left')))} дней",
    ])

    if getattr(config, "AI_EXPLAIN_ENABLED", True):
        details.extend(["", _plain_explain(alert)])

    # Telegram's expandable blockquote is natively collapsed in supported clients.
    header.extend([
        "",
        "<blockquote expandable>" + "\n".join(details) + "</blockquote>",
    ])

    return "\n".join(header)
