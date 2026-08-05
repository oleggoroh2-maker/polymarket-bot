"""Robust outcome normalization for AI training and model comparison.

Raw market returns remain untouched in the database and user-facing audit.
Training/reporting code can use bounded and signed-log transforms so tiny-price
markets cannot dominate averages with four-digit percentage moves.
"""

from __future__ import annotations

import math
from typing import Any

import config

CAP_PERCENT = float(getattr(config, "RESULT_RETURN_CAP_PERCENT", 100.0))
LOG_SCALE_PERCENT = float(getattr(config, "RESULT_LOG_SCALE_PERCENT", 100.0))
NORMALIZED_CAP_PERCENT = float(
    getattr(config, "RESULT_NORMALIZED_CAP_PERCENT", 100.0)
)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def capped_return_percent(value: Any, cap_percent: float | None = None) -> float:
    """Clip a signed percentage return symmetrically."""
    cap = abs(_finite(cap_percent, CAP_PERCENT)) if cap_percent is not None else abs(CAP_PERCENT)
    if cap <= 0:
        return _finite(value)
    return max(-cap, min(cap, _finite(value)))


def signed_log_return_percent(
    value: Any,
    scale_percent: float | None = None,
    cap_percent: float | None = None,
) -> float:
    """Compress extreme percentage returns while preserving sign and ordering.

    Formula: sign(r) * scale * ln(1 + |r| / scale), then optional clipping.
    A 100% raw move becomes about 69.3 normalized points with scale=100.
    """
    raw = _finite(value)
    scale = abs(_finite(scale_percent, LOG_SCALE_PERCENT)) if scale_percent is not None else abs(LOG_SCALE_PERCENT)
    if scale <= 0:
        result = raw
    else:
        result = math.copysign(scale * math.log1p(abs(raw) / scale), raw)
    cap = (
        abs(_finite(cap_percent, NORMALIZED_CAP_PERCENT))
        if cap_percent is not None
        else abs(NORMALIZED_CAP_PERCENT)
    )
    if cap > 0:
        result = max(-cap, min(cap, result))
    return result


def normalized_training_return(value: Any) -> float:
    """Canonical bounded return used by diagnostic/training modules."""
    return signed_log_return_percent(value)


def entry_price_bucket(entry_price: Any) -> str:
    """Return a readable bucket for a Polymarket decimal price (0..1)."""
    price = max(0.0, _finite(entry_price))
    cents = price * 100.0
    if cents < 1.0:
        return "<1¢"
    if cents < 5.0:
        return "1–5¢"
    if cents < 20.0:
        return "5–20¢"
    if cents < 50.0:
        return "20–50¢"
    return "≥50¢"
