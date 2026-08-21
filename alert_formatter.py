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
    similarity_samples = int(_num(alert.get("similarity_samples")))
    similarity_average = _optional_num(alert.get("similarity_average"))
    similarity_strong = _optional_num(alert.get("similarity_strong_rate"))
    similarity_continuation = _optional_num(alert.get("similarity_continuation_rate"))
    similarity_return = _optional_num(alert.get("similarity_average_return"))
    similarity_best_title = alert.get("similarity_best_title")
    similarity_best_return = _optional_num(alert.get("similarity_best_return"))
    signal_confidence = _optional_num(alert.get("signal_confidence"))
    final_signal_score = _optional_num(alert.get("final_signal_score"))
    final_signal_label = escape(str(alert.get("final_signal_label") or ""))
    final_signal_components = alert.get("final_signal_components") or []
    ev_estimate = _optional_num(alert.get("ev_estimate_percent"))
    ev_label = escape(str(alert.get("ev_label") or ""))
    risk_score_v1 = _optional_num(alert.get("risk_score"))
    risk_label_v1 = escape(str(alert.get("risk_label") or ""))
    ev_probability = _optional_num(alert.get("ev_continuation_probability"))
    confidence_tier = escape(str(alert.get("confidence_label") or alert.get("confidence_tier") or ""))
    confidence_components = alert.get("confidence_components") or []
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
    if final_signal_score is not None:
        suffix = f" · {final_signal_label}" if final_signal_label else ""
        header.append(f"🔥 Final Signal: <b>{final_signal_score:.0f}/100</b>{suffix}")
    if ev_estimate is not None and risk_score_v1 is not None:
        header.append(f"💰 EV: <b>{ev_estimate:+.1f}%</b> · {ev_label}  |  🛡 Risk: <b>{risk_score_v1:.0f}/100</b> · {risk_label_v1}")
    if signal_confidence is not None:
        suffix = f" · {confidence_tier}" if confidence_tier else ""
        header.append(f"🎯 Confidence: <b>{signal_confidence:.0f}/100</b>{suffix}")
    header.append("")

    if change is not None:
        header.append(f"🟢 Цена {_signed(change)}")
    if volume_change is not None:
        header.append(f"{_factor_icon(volume_change)} Объём {_signed(volume_change)}")
    if liquidity_change is not None:
        header.append(f"{_factor_icon(liquidity_change)} Ликвидность {_signed(liquidity_change)}")
    if ml is not None:
        header.append(f"{_ml_icon(ml)} ML {ml * 100:.1f}%")

    if similarity_samples and similarity_strong is not None:
        history_text = f"🧠 Похожие: <b>{similarity_strong:.1f}%</b> сильных из {similarity_samples}"
        if similarity_average is not None:
            history_text += f" · сходство {similarity_average:.0f}%"
        header.extend(["", history_text])
    elif samples and strong_rate is not None:
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
    ]

    if final_signal_score is not None:
        details.extend([
            "",
            "<b>🔥 FINAL SIGNAL ENGINE v1</b>",
            f"Final Signal: {final_signal_score:.1f}/100",
            f"Уровень: {final_signal_label or '—'}",
            "Режим: live scoring — Quality v3 остаётся фильтром отправки",
        ])
        for component in [item for item in final_signal_components if isinstance(item, dict)][:6]:
            points = _num(component.get("points"))
            icon = "🟢" if points > 0 else "🔴" if points < 0 else "⚪"
            details.append(f"{icon} {escape(str(component.get('label') or component.get('key') or 'Фактор'))}: {points:+.1f}")

    if ev_estimate is not None and risk_score_v1 is not None:
        details.extend([
            "",
            "<b>💰 EXPECTED VALUE + RISK v1</b>",
            f"EV: {ev_estimate:+.1f}% · {ev_label}",
            f"Risk: {risk_score_v1:.1f}/100 · {risk_label_v1}",
            f"Оценка продолжения: {ev_probability:.1f}%" if ev_probability is not None else "Оценка продолжения: —",
            f"Решение: {escape(str(alert.get('ev_risk_reason') or '—'))}",
        ])

    if signal_confidence is not None:
        details.extend([
            "",
            "<b>🎯 SIGNAL CONFIDENCE</b>",
            f"Confidence: {signal_confidence:.1f}/100",
            f"Уровень: {confidence_tier or '—'}",
            "Режим: Shadow — не влияет на отправку",
        ])
        for component in sorted(
            (item for item in confidence_components if isinstance(item, dict)),
            key=lambda item: abs(_num(item.get("points"))),
            reverse=True,
        )[:6]:
            points = _num(component.get("points"))
            icon = "🟢" if points > 0 else "🔴" if points < 0 else "⚪"
            details.append(
                f"{icon} {escape(str(component.get('label') or component.get('key') or 'Фактор'))}: {points:+.1f}"
            )

    details.extend([
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
    ])

    structure = _structure_lines(alert)
    if structure:
        details.extend(["", *structure])

    if similarity_samples and similarity_strong is not None:
        details.extend([
            "",
            "<b>🧠 ПОХОЖИЕ СИГНАЛЫ</b>",
            f"Найдено: {similarity_samples}",
            f"Среднее сходство: {similarity_average:.1f}%" if similarity_average is not None else "Среднее сходство: —",
            f"Сильное продолжение: {similarity_strong:.1f}%",
            f"Любое продолжение: {similarity_continuation:.1f}%" if similarity_continuation is not None else "Любое продолжение: —",
            f"Средний результат: {_signed(similarity_return)}" if similarity_return is not None else "Средний результат: —",
        ])
        if similarity_best_title and similarity_best_return is not None:
            details.extend([
                f"Лучший аналог: {escape(_short_title(similarity_best_title, 70))}",
                f"Результат аналога: {_signed(similarity_best_return)}",
            ])

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
