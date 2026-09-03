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
from memory_engine import classify_outcome, get_memory_stats
from ai_simulator import record_simulator_signal
from confidence_engine import record_confidence_signal

logger = logging.getLogger(__name__)

CHECKPOINTS_MINUTES = (30, 60, 180, 360, 720, 1440, 4320)
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
                volume REAL,
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
                status TEXT,
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

            CREATE TABLE IF NOT EXISTS ai_schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

        columns = {
            str(row[1])
            for row in cursor.execute(
                "PRAGMA table_info(market_snapshots)"
            ).fetchall()
        }
        if "volume" not in columns:
            cursor.execute(
                "ALTER TABLE market_snapshots ADD COLUMN volume REAL"
            )

        outcome_columns = {
            str(row[1])
            for row in cursor.execute(
                "PRAGMA table_info(signal_outcomes)"
            ).fetchall()
        }
        if "status" not in outcome_columns:
            cursor.execute(
                "ALTER TABLE signal_outcomes ADD COLUMN status TEXT"
            )

        memory_version = cursor.execute(
            "SELECT value FROM ai_schema_meta WHERE key = ?",
            ("memory_outcome_version",),
        ).fetchone()
        if not memory_version or str(memory_version[0]) != "2.2":
            rows = cursor.execute(
                """
                SELECT o.signal_id, o.checkpoint_minutes, o.return_percent,
                       s.alert_type, s.metadata_json
                FROM signal_outcomes o
                JOIN ai_signals s ON s.signal_id = o.signal_id
                """
            ).fetchall()
            for signal_id, checkpoint, market_return, alert_type, metadata_json in rows:
                try:
                    metadata = json.loads(metadata_json or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                direction = _expected_direction(str(alert_type), metadata)
                directional = float(market_return) * direction
                status = classify_outcome(directional)
                success = (
                    int(status == "SUCCESS")
                    if int(checkpoint) == TRAINING_CHECKPOINT_MINUTES
                    else None
                )
                cursor.execute(
                    """
                    UPDATE signal_outcomes
                    SET directional_return_percent = ?, status = ?, success = ?
                    WHERE signal_id = ? AND checkpoint_minutes = ?
                    """,
                    (directional, status, success, signal_id, checkpoint),
                )
            cursor.execute(
                """
                INSERT INTO ai_schema_meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("memory_outcome_version", "2.2"),
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
def _percent_change(
    current: float,
    previous: float,
) -> float:
    if previous <= 0:
        return 100.0 if current > 0 else 0.0

    return abs(current - previous) / previous * 100.0


def _snapshot_interval_minutes(score: int) -> int:
    if score >= getattr(config, "SNAPSHOT_HIGH_SCORE", 70):
        return getattr(
            config,
            "SNAPSHOT_HIGH_INTERVAL_MINUTES",
            5,
        )

    if score >= getattr(config, "SNAPSHOT_MEDIUM_SCORE", 40):
        return getattr(
            config,
            "SNAPSHOT_MEDIUM_INTERVAL_MINUTES",
            30,
        )

    return getattr(
        config,
        "SNAPSHOT_LOW_INTERVAL_MINUTES",
        120,
    )


def _get_strongest_market_move(
    market: dict[str, Any],
) -> float:
    changes = [
        market.get("change_5m"),
        market.get("change_15m"),
        market.get("change_1h"),
        market.get("change_24h"),
    ]

    return max(
        (
            abs(float(value))
            for value in changes
            if value is not None
        ),
        default=0.0,
    )


def save_market_snapshots(
    markets: list[dict[str, Any]],
) -> None:
    ensure_ai_schema()

    if not markets:
        return

    now = _now().replace(
        second=0,
        microsecond=0,
    )
    captured_at = now.isoformat()

    market_ids = [
        str(market.get("id") or "")
        for market in markets
        if market.get("id")
    ]

    latest_by_market: dict[str, dict[str, Any]] = {}

    with closing(get_connection()) as connection:
        # SQLite обычно ограничивает количество параметров,
        # поэтому загружаем последние снимки частями.
        chunk_size = 500

        for start in range(0, len(market_ids), chunk_size):
            chunk = market_ids[start:start + chunk_size]

            if not chunk:
                continue

            placeholders = ",".join("?" for _ in chunk)

            rows = connection.execute(
                f"""
                SELECT
                    snapshot.market_id,
                    snapshot.captured_at,
                    snapshot.price,
                    snapshot.liquidity,
                    snapshot.volume,
                    snapshot.score,
                    snapshot.momentum,
                    snapshot.change_5m,
                    snapshot.change_15m,
                    snapshot.change_1h,
                    snapshot.change_24h
                FROM market_snapshots AS snapshot
                INNER JOIN (
                    SELECT
                        market_id,
                        MAX(captured_at) AS latest_captured_at
                    FROM market_snapshots
                    WHERE market_id IN ({placeholders})
                    GROUP BY market_id
                ) AS latest
                    ON latest.market_id = snapshot.market_id
                    AND latest.latest_captured_at = snapshot.captured_at
                """,
                chunk,
            ).fetchall()

            for row in rows:
                latest_by_market[str(row[0])] = {
                    "captured_at": row[1],
                    "price": float(row[2] or 0),
                    "liquidity": float(row[3] or 0),
                    "volume": (
                        float(row[4]) if row[4] is not None else None
                    ),
                    "score": int(row[5] or 0),
                    "momentum": str(row[6] or ""),
                    "change_5m": row[7],
                    "change_15m": row[8],
                    "change_1h": row[9],
                    "change_24h": row[10],
                }

        rows_to_insert: list[tuple[Any, ...]] = []

        for market in markets:
            market_id = str(market.get("id") or "")

            if not market_id:
                continue

            price = float(market.get("price") or 0)
            liquidity = float(
                market.get("liquidity") or 0
            )
            raw_volume = market.get("volume")
            volume = (
                float(raw_volume)
                if raw_volume is not None
                else None
            )
            score = int(market.get("score") or 0)
            momentum = str(
                market.get("momentum") or ""
            )

            previous = latest_by_market.get(market_id)
            should_save = previous is None

            if previous is not None:
                price_change = _percent_change(
                    price,
                    previous["price"],
                )
                liquidity_change = _percent_change(
                    liquidity,
                    previous["liquidity"],
                )

                price_changed = price_change >= getattr(
                    config,
                    "SNAPSHOT_FORCE_PRICE_CHANGE_PERCENT",
                    2.0,
                )

                liquidity_changed = (
                    liquidity_change
                    >= getattr(
                        config,
                        "SNAPSHOT_FORCE_LIQUIDITY_CHANGE_PERCENT",
                        5.0,
                    )
                )

                previous_volume = previous.get("volume")
                volume_change = (
                    _percent_change(volume, previous_volume)
                    if volume is not None
                    and previous_volume is not None
                    else 0.0
                )
                volume_changed = (
                    volume_change
                    >= getattr(
                        config,
                        "SNAPSHOT_FORCE_VOLUME_CHANGE_PERCENT",
                        5.0,
                    )
                )

                score_change = abs(
                    score - previous["score"]
                )

                score_changed = (
                    score_change
                    >= int(
                        getattr(
                            config,
                            "SNAPSHOT_FORCE_SCORE_CHANGE",
                            4,
                        )
                    )
                )

                momentum_changed = (
                    momentum != previous["momentum"]
                )

                current_strongest_move = (
                    _get_strongest_market_move(market)
                )
                previous_strongest_move = (
                    _get_strongest_market_move(previous)
                )
                strong_move_threshold = float(
                    getattr(
                        config,
                        "SNAPSHOT_FORCE_MARKET_MOVE_PERCENT",
                        20.0,
                    )
                )
                strong_move_crossed = (
                    current_strongest_move
                    >= strong_move_threshold
                    and previous_strongest_move
                    < strong_move_threshold
                )

                try:
                    previous_time = datetime.fromisoformat(
                        str(previous["captured_at"])
                    )

                    if (
                        previous_time.tzinfo is None
                        and now.tzinfo is not None
                    ):
                        previous_time = previous_time.replace(
                            tzinfo=now.tzinfo
                        )

                    minutes_since_last = (
                        now - previous_time
                    ).total_seconds() / 60
                except (TypeError, ValueError):
                    minutes_since_last = float("inf")

                interval_reached = (
                    minutes_since_last
                    >= _snapshot_interval_minutes(score)
                )

                should_save = any([
                    price_changed,
                    liquidity_changed,
                    volume_changed,
                    score_changed,
                    momentum_changed,
                    strong_move_crossed,
                    interval_reached,
                ])

            if not should_save:
                continue

            rows_to_insert.append(
                (
                    market_id,
                    captured_at,
                    price,
                    liquidity,
                    volume,
                    int(market.get("days_left") or 0),
                    score,
                    market.get("category"),
                    market.get("momentum"),
                    market.get("change_5m"),
                    market.get("change_15m"),
                    market.get("change_1h"),
                    market.get("change_24h"),
                )
            )

        if not rows_to_insert:
            return

        connection.executemany(
            """
            INSERT OR IGNORE INTO market_snapshots (
                market_id,
                captured_at,
                price,
                liquidity,
                volume,
                days_left,
                score,
                category,
                momentum,
                change_5m,
                change_15m,
                change_1h,
                change_24h
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows_to_insert,
        )

        connection.commit()


def get_market_metrics_before_many(
    market_id: str,
    minutes_values: tuple[int, ...] = (5, 15, 60, 1440),
) -> dict[int, dict[str, Optional[float]]]:
    """Return real historical liquidity and volume values per timeframe."""
    ensure_ai_schema()
    now = _now()
    oldest_target = now - timedelta(minutes=max(minutes_values))

    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT captured_at, liquidity, volume
            FROM market_snapshots
            WHERE market_id = ?
              AND captured_at <= ?
            ORDER BY captured_at DESC
            LIMIT 2000
            """,
            (market_id, now.isoformat()),
        ).fetchall()

    parsed: list[tuple[datetime, float, Optional[float]]] = []
    for captured_at, liquidity, volume in rows:
        try:
            captured = datetime.fromisoformat(str(captured_at))
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        parsed.append((
            captured,
            float(liquidity or 0),
            float(volume) if volume is not None else None,
        ))
        if captured <= oldest_target and len(parsed) > 1:
            break

    result: dict[int, dict[str, Optional[float]]] = {}
    for minutes in minutes_values:
        target = now - timedelta(minutes=minutes)
        match = next((row for row in parsed if row[0] <= target), None)
        result[minutes] = {
            "liquidity": match[1] if match else None,
            "volume": match[2] if match else None,
        }
    return result


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
        "price_change_percent": alert.get("change_percent"),
        "volume_change_percent": alert.get("volume_change_percent"),
        "liquidity_change_percent": alert.get("liquidity_change_percent"),
        "momentum": alert.get("momentum"),
        "ai_quality": assessment.get("ai_quality"),
        "ai_risk": assessment.get("ai_risk"),
        "ml_probability": assessment.get("ml_probability"),
        "event_slug": alert.get("event_slug"),
        "event_title": alert.get("event_title"),
        "market_group_key": alert.get("market_group_key"),
        "market_group_title": alert.get("market_group_title"),
        "market_group_size": alert.get("market_group_size"),
        "market_group_suppressed": alert.get("market_group_suppressed"),
        "similarity_samples": alert.get("similarity_samples"),
        "similarity_average": alert.get("similarity_average"),
        "similarity_strong_rate": alert.get("similarity_strong_rate"),
        "similarity_continuation_rate": alert.get("similarity_continuation_rate"),
        "similarity_average_return": alert.get("similarity_average_return"),
        "calibration_confidence": alert.get("calibration_confidence"),
        "calibration_tier": alert.get("calibration_tier"),
        "signal_confidence": alert.get("signal_confidence"),
        "confidence_tier": alert.get("confidence_tier"),
        "confidence_components": alert.get("confidence_components"),
        "price_bucket": alert.get("price_bucket"),
        "price_intelligence_adjustment": alert.get("price_intelligence_adjustment"),
        "price_intelligence_samples": alert.get("price_intelligence_samples"),
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

    try:
        record_simulator_signal(signal_id, alert, assessment)
    except Exception:
        logger.exception("AI Simulator failed to record signal %s", signal_id)

    try:
        record_confidence_signal(signal_id, alert)
    except Exception:
        logger.exception("Confidence Engine failed to record signal %s", signal_id)

    return signal_id


def _expected_direction(alert_type: str, market: dict[str, Any]) -> int:
    """Return 1 for bullish signals and -1 for bearish signals."""
    normalized = str(alert_type or "").upper()
    if "DIP" in normalized:
        return -1
    if "PUMP" in normalized:
        return 1

    momentum = str(market.get("momentum") or "").upper()
    if "DIP" in momentum or "BEAR" in momentum or "FALL" in momentum:
        return -1
    return 1


def update_outcomes(markets: list[dict[str, Any]]) -> int:
    """Write due checkpoints using prices from the latest completed scan."""
    ensure_ai_schema()
    current = {str(market["id"]): market for market in markets}
    now = _now()
    inserted = 0

    with closing(get_connection()) as connection:
        signals = connection.execute(
            """
            SELECT signal_id, market_id, alert_type, created_at, entry_price,
                   metadata_json
            FROM ai_signals
            WHERE created_at >= ?
            """,
            ((now - timedelta(days=8)).isoformat(),),
        ).fetchall()

        for (
            signal_id,
            market_id,
            alert_type,
            created_at_raw,
            entry_price,
            metadata_json,
        ) in signals:
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
            entry_price = float(entry_price)
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

            try:
                original_signal = json.loads(metadata_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                original_signal = {}

            direction = _expected_direction(
                str(alert_type),
                original_signal or market,
            )
            # Signed result at the checkpoint: positive means movement in the
            # expected signal direction, negative means movement against it.
            directional_return = return_percent * direction

            for checkpoint in CHECKPOINTS_MINUTES:
                if elapsed_minutes < checkpoint:
                    continue

                status = classify_outcome(directional_return)
                success = int(status == "SUCCESS") if checkpoint == TRAINING_CHECKPOINT_MINUTES else None
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO signal_outcomes (
                        signal_id, checkpoint_minutes, measured_at, price,
                        return_percent, directional_return_percent,
                        max_price, min_price, success, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal_id, checkpoint, now.isoformat(), current_price,
                        return_percent, directional_return, max_price, min_price,
                        success, status,
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
    memory_24h = get_memory_stats(1440)
    return {
        "snapshots": int(snapshots),
        "signals": int(signals),
        "outcomes": int(outcomes),
        "training_samples": int(training),
        "min_training_samples": MIN_TRAINING_SAMPLES,
        "model_ready": model is not None,
        "model_samples": int(model.get("samples", 0)) if model else 0,
        "validation_accuracy": model.get("validation_accuracy") if model else None,
        "memory_24h": memory_24h,
    }
