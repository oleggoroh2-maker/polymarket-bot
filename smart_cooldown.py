from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from database import get_smart_cooldown_records, save_smart_cooldown_records
from market_group_engine import get_market_group_key


SMART_COOLDOWN_ENABLED = bool(getattr(config, "SMART_COOLDOWN_ENABLED", True))
SMART_COOLDOWN_HOURS = float(getattr(config, "SMART_COOLDOWN_HOURS", 24.0))
SMART_COOLDOWN_PRICE_MOVE_PERCENT = float(
    getattr(config, "SMART_COOLDOWN_PRICE_MOVE_PERCENT", 20.0)
)
SMART_COOLDOWN_QUESTION_MATCH = bool(
    getattr(config, "SMART_COOLDOWN_QUESTION_MATCH", True)
)


@dataclass(frozen=True)
class CooldownDecision:
    blocked: bool
    reason: str = ""
    identity_type: str = ""
    elapsed_hours: float | None = None
    remaining_hours: float | None = None
    price_move_percent: float | None = None
    previous_price: float | None = None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalize(value: Any) -> str:
    text = _text(value).lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-z0-9а-яё]+", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def _hash_question(value: Any) -> str:
    normalized = _normalize(value)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def _signal_family(alert: dict[str, Any]) -> str:
    alert_type = _text(alert.get("alert_type")).upper()
    label = _text(alert.get("alert_label")).upper()
    momentum = _text(alert.get("momentum")).upper()

    if "OPPORTUNITY" in alert_type or "OPPORTUNITY" in label:
        return "OPPORTUNITY"
    if "DIP" in alert_type or "DIP" in label or "DIP" in momentum:
        return "DIP"
    if "PUMP" in alert_type or "PUMP" in label or "PUMP" in momentum:
        return "PUMP"
    return alert_type or "OTHER"


def _current_price(alert: dict[str, Any]) -> float:
    for key in ("current_price", "price"):
        try:
            value = float(alert.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def build_cooldown_identities(alert: dict[str, Any]) -> list[tuple[str, str]]:
    identities: list[tuple[str, str]] = []

    market_id = _normalize(alert.get("id"))
    if market_id:
        identities.append(("market_id", market_id))

    for key in ("event_id", "eventId"):
        event_id = _normalize(alert.get(key))
        if event_id:
            identities.append(("event_id", event_id))
            break

    event = alert.get("event")
    if isinstance(event, dict):
        event_id = _normalize(event.get("id"))
        if event_id and ("event_id", event_id) not in identities:
            identities.append(("event_id", event_id))

    event_slug = _normalize(alert.get("event_slug") or alert.get("eventSlug"))
    if not event_slug and isinstance(event, dict):
        event_slug = _normalize(event.get("slug"))
    if event_slug:
        identities.append(("event_slug", event_slug))

    market_slug = _normalize(alert.get("market_slug") or alert.get("slug"))
    if market_slug:
        identities.append(("market_slug", market_slug))

    group_key = _normalize(alert.get("market_group_key") or get_market_group_key(alert))
    if group_key:
        identities.append(("group_key", group_key))

    if SMART_COOLDOWN_QUESTION_MATCH:
        question_hash = _hash_question(alert.get("title") or alert.get("question"))
        if question_hash:
            identities.append(("question_hash", question_hash))

    # Preserve order while removing duplicates.
    return list(dict.fromkeys(identities))


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _price_move_percent(current: float, previous: float) -> float | None:
    if current <= 0 or previous <= 0:
        return None
    return ((current - previous) / previous) * 100.0


def check_smart_cooldown(alert: dict[str, Any]) -> CooldownDecision:
    """Return whether an alert should be suppressed.

    A repeat is allowed inside the cooldown window only when the market price
    has moved by at least SMART_COOLDOWN_PRICE_MOVE_PERCENT from the most
    recent matching alert. Matching is performed across market id, event id,
    event/market slug, event group and normalized question.
    """
    if not SMART_COOLDOWN_ENABLED:
        return CooldownDecision(blocked=False)

    identities = build_cooldown_identities(alert)
    if not identities:
        return CooldownDecision(blocked=False)

    family = _signal_family(alert)
    records = get_smart_cooldown_records(identities, family)
    if not records:
        return CooldownDecision(blocked=False)

    now = datetime.now(timezone.utc)
    current_price = _current_price(alert)
    window = timedelta(hours=SMART_COOLDOWN_HOURS)

    active: list[tuple[datetime, dict[str, Any]]] = []
    for record in records:
        created_at = _parse_time(record.get("created_at"))
        if created_at is None or now - created_at >= window:
            continue
        active.append((created_at, record))

    if not active:
        return CooldownDecision(blocked=False)

    # Use the newest matching alert as the repeat reference.
    created_at, record = max(active, key=lambda item: item[0])
    previous_price = float(record.get("price") or 0.0)
    move = _price_move_percent(current_price, previous_price)

    if move is not None and abs(move) >= SMART_COOLDOWN_PRICE_MOVE_PERCENT:
        return CooldownDecision(
            blocked=False,
            reason="significant_price_move",
            identity_type=str(record.get("identity_type") or ""),
            elapsed_hours=(now - created_at).total_seconds() / 3600.0,
            price_move_percent=move,
            previous_price=previous_price,
        )

    elapsed = (now - created_at).total_seconds() / 3600.0
    remaining = max(0.0, SMART_COOLDOWN_HOURS - elapsed)
    return CooldownDecision(
        blocked=True,
        reason="cooldown_active",
        identity_type=str(record.get("identity_type") or ""),
        elapsed_hours=elapsed,
        remaining_hours=remaining,
        price_move_percent=move,
        previous_price=previous_price or None,
    )


def register_smart_cooldown(alert: dict[str, Any]) -> None:
    if not SMART_COOLDOWN_ENABLED:
        return

    identities = build_cooldown_identities(alert)
    if not identities:
        return

    save_smart_cooldown_records(
        identities=identities,
        signal_family=_signal_family(alert),
        market_id=_text(alert.get("id")),
        event_id=_text(alert.get("event_id") or alert.get("eventId")),
        price=_current_price(alert),
        title=_text(alert.get("title") or alert.get("question")),
    )
