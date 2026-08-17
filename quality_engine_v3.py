"""Quality Engine v3 — live high-precision alert gate.

The gate intentionally prefers fewer alerts. It uses only rules that were
already identified before activation; it does not query historical outcomes.
AI Memory still records every candidate before this gate, so rejected signals
remain available for unbiased analytics.
"""
from __future__ import annotations

from typing import Any


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def evaluate_quality_v3(alert: dict[str, Any]) -> tuple[bool, str, int]:
    """Return (pass, reason, confirmations) for the live precision gate."""
    score = float(alert.get("score") or 0.0)
    alert_type = str(alert.get("alert_type") or "").upper()
    category = str(alert.get("category") or "OTHER").upper()

    price = _num(alert.get("current_price"))
    if price is None:
        price = _num(alert.get("price"))

    similarity = _num(alert.get("similarity_average"))
    if similarity is not None and 0 <= similarity <= 1:
        similarity *= 100.0

    liquidity_change = _num(alert.get("liquidity_change_percent"))

    confirmations = 0
    confirmations += int(price is not None and 0.01 <= price < 0.05)
    confirmations += int(similarity is not None and 80 <= similarity < 90)
    confirmations += int(liquidity_change is not None and liquidity_change >= 30)

    # Keep every non-AI/TECH category on the original alert routing.
    # V3 precision filtering is applied only to AI/TECH signals.
    if "AI/TECH" not in category:
        return True, "ORIGINAL_CATEGORY_FLOW", confirmations

    if "OPPORTUNITY" in alert_type:
        return False, "OPPORTUNITY", confirmations
    if not (60 <= score <= 74):
        return False, "SCORE_BUCKET", confirmations
    if confirmations < 1:
        return False, "NO_CONFIRMATION", confirmations
    return True, "QUALITY_V3_AI_TECH", confirmations
