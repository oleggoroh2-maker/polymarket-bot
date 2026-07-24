"""AI data layer and shadow scoring for the Polymarket bot."""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import config
from database import get_connection
from feature_engine import calculate_features, calculate_rule_assessment
from ml_engine import load_model, predict_probability, train_model

logger = logging.getLogger(__name__)

CHECKPOINTS_MINUTES = (15, 60, 360, 1440, 4320)
TRAINING_CHECKPOINT_MINUTES = 1440
SUCCESS_MOVE_PERCENT = float(getattr(config, "AI_SUCCESS_MOVE_PERCENT", 20.0))
MIN_TRAINING_SAMPLES = int(getattr(config, "AI_MIN_TRAINING_SAMPLES", 200))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_ai_schema() -> None:
    with closing(get_connection()) as connection:
        cursor = connection.cursor()
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                price REAL NOT NULL,
                liquidity REAL NOT NULL,
                days_left INTEGER NOT NULL,
                score INTEGER NOT NULL,
                category TEXT,
                momentum TEXT,
                change_5m REAL,
                change_15m REAL,
                change_1h REAL,
                change_24h REAL,
                UNIQUE (market_id, captured_at)
            );

            CREATE INDEX IF NOT EXISTS idx_market_snapshots_market_time
            ON market_snapshots (market_id, captured_at);

            CREATE TABLE IF NOT EXISTS ai_signals (
                signal_id TEXT PRIMARY KEY,
                market_id TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                alert_label TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                entry_price REAL NOT NULL,
                liquidity REAL NOT NULL,
                days_left INTEGER NOT NULL,
                category TEXT,
                base_score INTEGER NOT NULL,
                ai_quality INTEGER NOT NULL,
                ai_risk INTEGER NOT NULL,
                ml_probability REAL,
                features_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                UNIQUE (market_id, alert_type, created_at)
            );

            CREATE INDEX IF NOT EXISTS idx_ai_signals_market_time
            ON ai_signals (market_id, created_at);

            CREATE TABLE IF NOT EXISTS signal_outcomes (
                signal_id TEXT NOT NULL,
                checkpoint_minutes INTEGER NOT NULL,
                measured_at TEXT NOT NULL,
                price REAL NOT NULL,
                return_percent REAL NOT NULL,
                directional_return_percent REAL NOT NULL,
                max_price REAL NOT NULL,
                min_price REAL NOT NULL,
                success INTEGER,
                PRIMARY KEY (signal_id, checkpoint_minutes),
                FOREIGN KEY (signal_id) REFERENCES ai_signals(signal_id)
            );

            CREATE TABLE IF NOT EXISTS ml_training_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                trained INTEGER NOT NULL,
                samples INTEGER NOT NULL,
                validation_accuracy REAL,
                details_json TEXT NOT NULL
            );
            """
        )
        connection.commit()


def assess_signal(signal: dict[str, Any]) -> dict[str, Any]:
    assessment = calculate_rule_assessment(signal)
    probability = predict_probability(assessment["features"])
    assessment["ml_probability"] = probability
    assessment["ml_status"] = "ready" if probability is not None else "collecting_data"
    return assessment


def enrich_signal(signal: dict[str, Any]) -> dict[str, Any]:
    try:
        assessment = assess_signal(signal)
        return {**signal, **assessment}
    except Exception:
        logger.exception("AI assessment failed for market %s", signal.get("id"))
        return {
            **signal,
            "ai_quality": None,
            "ai_risk": None,
            "ml_probability": None,
            "ml_status": "error",
            "features": {},
            "reasons": [],
        }


def save_market_snapshots(markets: list[dict[str, Any]]) -> None:
    ensure_ai_schema()
    captured_at = _now().replace(second=0, microsecond=0).isoformat()
    rows = []
    for market in markets:
        rows.append((
            str(market["id"]), captured_at, float(market["price"]),
            float(market.get("liquidity") or 0), int(market.get("days_left") or 0),
            int(market.get("score") or 0), market.get("category"), market.get("momentum"),
            market.get("change_5m"), market.get("change_15m"),
            market.get("change_1h"), market.get("change_24h"),
        ))

    with closing(get_connection()) as connection:
        connection.executemany(
            """
            INSERT OR IGNORE INTO market_snapshots (
                market_id, captured_at, price, liquidity, days_left, score,
                category, momentum, change_5m, change_15m, change_1h, change_24h
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.commit()


def record_alert(alert: dict[str, Any]) -> str:
    ensure_ai_schema()
    created_at = _now().isoformat()
    signal_id = str(uuid.uuid4())
    assessment = assess_signal(alert)

    metadata = {
        "timeframe": alert.get("timeframe"),
        "change_percent": alert.get("change_percent"),
        "old_price": alert.get("old_price"),
        "absolute_move": alert.get("absolute_move"),
        "url": alert.get("url"),
        "reasons": assessment.get("reasons", []),
    }

    with closing(get_connection()) as connection:
        connection.execute(
            """
            INSERT INTO ai_signals (
                signal_id, market_id, alert_type, alert_label, title, created_at,
                entry_price, liquidity, days_left, category, base_score,
                ai_quality, ai_risk, ml_probability, features_json, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_id, str(alert["id"]), str(alert["alert_type"]),
                str(alert["alert_label"]), str(alert["title"]), created_at,
                float(alert.get("current_price", alert["price"])),
                float(alert.get("liquidity") or 0), int(alert.get("days_left") or 0),
                alert.get("category"), int(alert.get("score") or 0),
                int(assessment["ai_quality"]), int(assessment["ai_risk"]),
                assessment.get("ml_probability"),
                json.dumps(assessment["features"], ensure_ascii=False),
                json.dumps(metadata, ensure_ascii=False),
            ),
        )
        connection.commit()

    return signal_id


def update_outcomes(markets: list[dict[str, Any]]) -> int:
    """Write due checkpoints using prices from the latest completed scan."""
    ensure_ai_schema()
    current = {str(market["id"]): market for market in markets}
    now = _now()
    inserted = 0

    with closing(get_connection()) as connection:
        signals = connection.execute(
            """
            SELECT signal_id, market_id, alert_type, created_at, entry_price
            FROM ai_signals
            WHERE created_at >= ?
            """,
            ((now - timedelta(days=8)).isoformat(),),
        ).fetchall()

        for signal_id, market_id, alert_type, created_at_raw, entry_price in signals:
            market = current.get(str(market_id))
            if market is None or not entry_price:
                continue
            try:
                created_at = datetime.fromisoformat(created_at_raw)
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue

            elapsed_minutes = (now - created_at).total_seconds() / 60.0
            current_price = float(market["price"])
            return_percent = ((current_price - entry_price) / entry_price) * 100.0
            extrema = connection.execute(
                """
                SELECT MAX(price), MIN(price)
                FROM market_snapshots
                WHERE market_id = ? AND captured_at >= ?
                """,
                (str(market_id), created_at.isoformat()),
            ).fetchone()
            max_price = float(extrema[0] if extrema and extrema[0] is not None else current_price)
            min_price = float(extrema[1] if extrema and extrema[1] is not None else current_price)
            max_return_percent = ((max_price - entry_price) / entry_price) * 100.0

            for checkpoint in CHECKPOINTS_MINUTES:
                if elapsed_minutes < checkpoint:
                    continue
                success = None
                if checkpoint == TRAINING_CHECKPOINT_MINUTES:
                    success = int(max_return_percent >= SUCCESS_MOVE_PERCENT)
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO signal_outcomes (
                        signal_id, checkpoint_minutes, measured_at, price,
                        return_percent, directional_return_percent,
                        max_price, min_price, success
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal_id, checkpoint, now.isoformat(), current_price,
                        return_percent, max_return_percent, max_price, min_price, success,
                    ),
                )
                inserted += cursor.rowcount

        connection.commit()

    return inserted


def get_training_samples() -> list[tuple[dict[str, float], int]]:
    ensure_ai_schema()
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT s.features_json, o.success
            FROM ai_signals s
            JOIN signal_outcomes o ON o.signal_id = s.signal_id
            WHERE o.checkpoint_minutes = ? AND o.success IS NOT NULL
            """,
            (TRAINING_CHECKPOINT_MINUTES,),
        ).fetchall()

    samples = []
    for features_json, success in rows:
        try:
            features = json.loads(features_json)
        except (TypeError, ValueError):
            continue
        samples.append((features, int(success)))
    return samples


def maybe_train_model() -> dict[str, Any]:
    samples = get_training_samples()
    sample_count = len(samples)
    if sample_count < MIN_TRAINING_SAMPLES:
        return {
            "trained": False,
            "reason": "not_enough_samples",
            "samples": sample_count,
        }

    existing = load_model()
    if existing is not None and sample_count < int(existing.get("samples", 0)) + 100:
        return {"trained": False, "reason": "model_current", "samples": sample_count}

    ensure_ai_schema()
    with closing(get_connection()) as connection:
        last_run = connection.execute(
            "SELECT samples FROM ml_training_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    # Do not retry a failed training attempt on every five-minute scan.
    if last_run is not None and sample_count < int(last_run[0]) + 25:
        return {"trained": False, "reason": "waiting_for_more_samples", "samples": sample_count}

    result = train_model(samples, min_samples=MIN_TRAINING_SAMPLES)
    with closing(get_connection()) as connection:
        connection.execute(
            """
            INSERT INTO ml_training_runs (
                created_at, trained, samples, validation_accuracy, details_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                _now().isoformat(), int(bool(result.get("trained"))),
                int(result.get("samples", sample_count)), result.get("validation_accuracy"),
                json.dumps(result, ensure_ascii=False),
            ),
        )
        connection.commit()
    return result


def process_scan(markets: list[dict[str, Any]]) -> None:
    """Safe shadow-mode processing. Never let AI stop the scanner."""
    try:
        save_market_snapshots(markets)
        update_outcomes(markets)
        maybe_train_model()
    except Exception:
        logger.exception("AI shadow processing failed")


def get_ai_stats() -> dict[str, Any]:
    ensure_ai_schema()
    with closing(get_connection()) as connection:
        snapshots = connection.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
        signals = connection.execute("SELECT COUNT(*) FROM ai_signals").fetchone()[0]
        outcomes = connection.execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0]
        training = connection.execute(
            "SELECT COUNT(*) FROM signal_outcomes WHERE checkpoint_minutes = ? AND success IS NOT NULL",
            (TRAINING_CHECKPOINT_MINUTES,),
        ).fetchone()[0]

    model = load_model()
    return {
        "snapshots": int(snapshots),
        "signals": int(signals),
        "outcomes": int(outcomes),
        "training_samples": int(training),
        "min_training_samples": MIN_TRAINING_SAMPLES,
        "model_ready": model is not None,
        "model_samples": int(model.get("samples", 0)) if model else 0,
        "validation_accuracy": model.get("validation_accuracy") if model else None,
    }
