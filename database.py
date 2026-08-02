import os
import sqlite3
import logging
import config
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "polymarket.db")


def get_connection() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)

    connection = sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False,
    )

    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")

    return connection


def init_db() -> None:
    with closing(get_connection()) as connection:
        cursor = connection.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS prices (
                id TEXT NOT NULL,
                price REAL NOT NULL,
                timestamp TEXT NOT NULL
            )
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_prices_market_time
            ON prices (id, timestamp)
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                market_id TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (market_id, alert_type)
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS market_group_alerts (
                group_key TEXT NOT NULL,
                alert_family TEXT NOT NULL,
                market_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (group_key, alert_family)
            )
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_market_group_alerts_time
            ON market_group_alerts (created_at)
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS smart_cooldowns (
                identity_type TEXT NOT NULL,
                identity_key TEXT NOT NULL,
                signal_family TEXT NOT NULL,
                market_id TEXT,
                event_id TEXT,
                price REAL NOT NULL DEFAULT 0,
                title TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (identity_type, identity_key, signal_family)
            )
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_smart_cooldowns_time
            ON smart_cooldowns (created_at)
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS smart_cooldown_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision TEXT NOT NULL,
                category TEXT NOT NULL,
                reason TEXT,
                identity_type TEXT,
                signal_family TEXT NOT NULL,
                market_id TEXT,
                event_id TEXT,
                title TEXT,
                price REAL NOT NULL DEFAULT 0,
                previous_price REAL,
                price_move_percent REAL,
                remaining_hours REAL,
                created_at TEXT NOT NULL
            )
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_smart_cooldown_events_time
            ON smart_cooldown_events (created_at)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_smart_cooldown_events_decision
            ON smart_cooldown_events (decision, created_at)
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                quality_mode TEXT NOT NULL DEFAULT 'ALL'
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS favorite_events (
                chat_id INTEGER NOT NULL,
                market_id TEXT NOT NULL,
                market_name TEXT NOT NULL,
                url TEXT,
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, market_id)
            )
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_favorite_events_chat
            ON favorite_events (chat_id, created_at)
            """
        )

        # AI Memory migration. The AI tables are created by ai_engine,
        # but an existing signal_outcomes table may need the new status field.
        outcome_table = cursor.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'signal_outcomes'
            LIMIT 1
            """
        ).fetchone()
        if outcome_table is not None:
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


        subscriber_columns = {
            str(row[1])
            for row in cursor.execute(
                "PRAGMA table_info(subscribers)"
            ).fetchall()
        }
        if "quality_mode" not in subscriber_columns:
            cursor.execute(
                "ALTER TABLE subscribers "
                "ADD COLUMN quality_mode TEXT NOT NULL DEFAULT 'ALL'"
            )

        connection.commit()


def save_price(market_id: str, price: float) -> None:
    now = datetime.now(timezone.utc)
    current_price = float(price)

    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT price, timestamp
            FROM prices
            WHERE id = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (market_id,),
        ).fetchone()

        if row is not None:
            previous_price = float(row[0])

            try:
                previous_time = datetime.fromisoformat(row[1])

                if previous_time.tzinfo is None:
                    previous_time = previous_time.replace(
                        tzinfo=timezone.utc
                    )
            except (TypeError, ValueError):
                previous_time = now - timedelta(hours=2)

            price_changed = abs(
                current_price - previous_price
            ) >= 0.000001

            hour_passed = (
                now - previous_time
            ) >= timedelta(hours=1)

            if not price_changed and not hour_passed:
                return

        connection.execute(
            """
            INSERT INTO prices (
                id,
                price,
                timestamp
            )
            VALUES (?, ?, ?)
            """,
            (
                market_id,
                current_price,
                now.isoformat(),
            ),
        )

        connection.commit()

def get_history(
    market_id: str,
    limit: int = 10,
) -> list[float]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT price
            FROM prices
            WHERE id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (
                market_id,
                limit,
            ),
        ).fetchall()

    return [float(row[0]) for row in rows]


def get_price_before(
    market_id: str,
    minutes: int,
) -> Optional[float]:
    target_time = (
        datetime.now(timezone.utc)
        - timedelta(minutes=minutes)
    ).isoformat()

    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT price
            FROM prices
            WHERE id = ?
              AND timestamp <= ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (
                market_id,
                target_time,
            ),
        ).fetchone()

    if row is None:
        return None

    return float(row[0])


def get_latest_price(
    market_id: str,
) -> Optional[float]:
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT price
            FROM prices
            WHERE id = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (market_id,),
        ).fetchone()

    if row is None:
        return None

    return float(row[0])


def cleanup_prices(days: int = 7) -> None:
    border = (
        datetime.now(timezone.utc)
        - timedelta(days=days)
    ).isoformat()

    with closing(get_connection()) as connection:
        connection.execute(
            """
            DELETE FROM prices
            WHERE timestamp < ?
            """,
            (border,),
        )

        connection.commit()


def alert_exists(
    market_id: str,
    alert_type: str,
) -> bool:
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT 1
            FROM alerts
            WHERE market_id = ?
              AND alert_type = ?
            LIMIT 1
            """,
            (
                market_id,
                alert_type,
            ),
        ).fetchone()

    return row is not None


def save_alert(
    market_id: str,
    alert_type: str,
) -> None:
    created_at = datetime.now(timezone.utc).isoformat()

    with closing(get_connection()) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO alerts (
                market_id,
                alert_type,
                created_at
            )
            VALUES (?, ?, ?)
            """,
            (
                market_id,
                alert_type,
                created_at,
            ),
        )

        connection.commit()


def get_alert_time(
    market_id: str,
    alert_type: str,
) -> Optional[datetime]:
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT created_at
            FROM alerts
            WHERE market_id = ?
              AND alert_type = ?
            LIMIT 1
            """,
            (
                market_id,
                alert_type,
            ),
        ).fetchone()

    if row is None:
        return None

    try:
        return datetime.fromisoformat(row[0])
    except (TypeError, ValueError):
        return None


def alert_on_cooldown(
    market_id: str,
    alert_type: str,
    cooldown_hours: int = 24,
) -> bool:
    alert_time = get_alert_time(
        market_id,
        alert_type,
    )

    if alert_time is None:
        return False

    if alert_time.tzinfo is None:
        alert_time = alert_time.replace(
            tzinfo=timezone.utc
        )

    cooldown_end = alert_time + timedelta(
        hours=cooldown_hours
    )

    return datetime.now(timezone.utc) < cooldown_end


def cleanup_alerts(days: int = 30) -> None:
    border = (
        datetime.now(timezone.utc)
        - timedelta(days=days)
    ).isoformat()

    with closing(get_connection()) as connection:
        connection.execute(
            """
            DELETE FROM alerts
            WHERE created_at < ?
            """,
            (border,),
        )
        connection.execute(
            """
            DELETE FROM market_group_alerts
            WHERE created_at < ?
            """,
            (border,),
        )
        connection.execute(
            """
            DELETE FROM smart_cooldowns
            WHERE created_at < ?
            """,
            (border,),
        )
        connection.execute(
            """
            DELETE FROM smart_cooldown_events
            WHERE created_at < ?
            """,
            (border,),
        )

        connection.commit()


def add_subscriber(
    chat_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()

    with closing(get_connection()) as connection:
        connection.execute(
            """
            INSERT INTO subscribers (
                chat_id,
                username,
                first_name,
                is_active,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                is_active = 1,
                updated_at = excluded.updated_at
            """,
            (
                int(chat_id),
                username,
                first_name,
                now,
                now,
            ),
        )
        connection.commit()


def disable_subscriber(chat_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()

    with closing(get_connection()) as connection:
        connection.execute(
            """
            UPDATE subscribers
            SET is_active = 0,
                updated_at = ?
            WHERE chat_id = ?
            """,
            (now, int(chat_id)),
        )
        connection.commit()


def is_subscriber_active(chat_id: int) -> bool:
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT is_active
            FROM subscribers
            WHERE chat_id = ?
            LIMIT 1
            """,
            (int(chat_id),),
        ).fetchone()

    return bool(row and row[0] == 1)


def get_active_subscribers() -> list[int]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT chat_id
            FROM subscribers
            WHERE is_active = 1
            ORDER BY created_at ASC
            """
        ).fetchall()

    return [int(row[0]) for row in rows]


def get_active_subscriber_profiles() -> list[dict[str, object]]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT chat_id, COALESCE(quality_mode, 'ALL')
            FROM subscribers
            WHERE is_active = 1
            ORDER BY created_at ASC
            """
        ).fetchall()
    return [
        {"chat_id": int(row[0]), "quality_mode": str(row[1] or "ALL").upper()}
        for row in rows
    ]


def set_subscriber_quality_mode(chat_id: int, mode: str) -> None:
    normalized = str(mode or "ALL").upper()
    if normalized not in {"ALL", "GOOD", "PREMIUM"}:
        raise ValueError("Unsupported quality mode")
    now = datetime.now(timezone.utc).isoformat()
    with closing(get_connection()) as connection:
        connection.execute(
            """
            UPDATE subscribers
            SET quality_mode = ?, updated_at = ?
            WHERE chat_id = ?
            """,
            (normalized, now, int(chat_id)),
        )
        connection.commit()


def get_subscriber_quality_mode(chat_id: int) -> str:
    with closing(get_connection()) as connection:
        row = connection.execute(
            "SELECT COALESCE(quality_mode, 'ALL') FROM subscribers WHERE chat_id = ?",
            (int(chat_id),),
        ).fetchone()
    return str(row[0] if row else "ALL").upper()


def get_subscribers_count() -> int:
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*)
            FROM subscribers
            WHERE is_active = 1
            """
        ).fetchone()

    return int(row[0] if row else 0)


def add_favorite_event(
    chat_id: int,
    market_id: str,
    market_name: str,
    url: Optional[str] = None,
    note: Optional[str] = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()

    with closing(get_connection()) as connection:
        connection.execute(
            """
            INSERT INTO favorite_events (
                chat_id,
                market_id,
                market_name,
                url,
                note,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, market_id) DO UPDATE SET
                market_name = excluded.market_name,
                url = excluded.url,
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            (
                int(chat_id),
                str(market_id),
                str(market_name),
                url,
                note,
                now,
                now,
            ),
        )
        connection.commit()


def get_favorite_events(chat_id: int) -> list[dict[str, Optional[str]]]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT market_id, market_name, url, note
            FROM favorite_events
            WHERE chat_id = ?
            ORDER BY created_at ASC
            """,
            (int(chat_id),),
        ).fetchall()

    return [
        {
            "market_id": str(row[0]),
            "market_name": str(row[1]),
            "url": row[2],
            "note": row[3],
        }
        for row in rows
    ]


def get_favorite_event(
    chat_id: int,
    market_id: str,
) -> Optional[dict[str, Optional[str]]]:
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT market_id, market_name, url, note
            FROM favorite_events
            WHERE chat_id = ?
              AND market_id = ?
            LIMIT 1
            """,
            (int(chat_id), str(market_id)),
        ).fetchone()

    if row is None:
        return None

    return {
        "market_id": str(row[0]),
        "market_name": str(row[1]),
        "url": row[2],
        "note": row[3],
    }


def delete_favorite_event(chat_id: int, market_id: str) -> bool:
    with closing(get_connection()) as connection:
        cursor = connection.execute(
            """
            DELETE FROM favorite_events
            WHERE chat_id = ?
              AND market_id = ?
            """,
            (int(chat_id), str(market_id)),
        )
        connection.commit()

    return cursor.rowcount > 0


def update_favorite_note(
    chat_id: int,
    market_id: str,
    note: Optional[str],
) -> bool:
    now = datetime.now(timezone.utc).isoformat()

    with closing(get_connection()) as connection:
        cursor = connection.execute(
            """
            UPDATE favorite_events
            SET note = ?,
                updated_at = ?
            WHERE chat_id = ?
              AND market_id = ?
            """,
            (note, now, int(chat_id), str(market_id)),
        )
        connection.commit()

    return cursor.rowcount > 0


# Совместимость со старым scanner.py
def cleanup(days: int = 30) -> None:
    cleanup_prices(days)


if __name__ == "__main__":
    init_db()
    cleanup_prices()
    cleanup_alerts()

    print(f"✅ База данных готова: {DB_PATH}")

logger = logging.getLogger(__name__)


def cleanup_old_database_data(
    run_vacuum: bool = False,
) -> dict[str, int | float | bool]:
    """
    Удаляет устаревшие данные.

    market_snapshots:
        хранение за последние 7 дней.

    prices:
        хранение за последние 30 дней.

    VACUUM выполняется только при достаточном количестве
    освобождённых страниц.
    """

    snapshots_days = int(
        getattr(
            config,
            "MARKET_SNAPSHOTS_RETENTION_DAYS",
            7,
        )
    )

    prices_days = int(
        getattr(
            config,
            "PRICES_RETENTION_DAYS",
            30,
        )
    )

    vacuum_min_free_ratio = float(
        getattr(
            config,
            "DATABASE_VACUUM_MIN_FREE_RATIO",
            0.15,
        )
    )

    result: dict[str, int | float | bool] = {
        "deleted_snapshots": 0,
        "deleted_prices": 0,
        "page_count": 0,
        "free_pages": 0,
        "free_ratio": 0.0,
        "vacuum_performed": False,
    }

    try:
        with closing(get_connection()) as connection:
            connection.execute(
                "PRAGMA busy_timeout = 60000"
            )

            snapshots_cursor = connection.execute(
                """
                DELETE FROM market_snapshots
                WHERE datetime(captured_at) <
                      datetime('now', ?)
                """,
                (f"-{snapshots_days} days",),
            )

            prices_cursor = connection.execute(
                """
                DELETE FROM prices
                WHERE datetime(timestamp) <
                      datetime('now', ?)
                """,
                (f"-{prices_days} days",),
            )

            result["deleted_snapshots"] = max(
                snapshots_cursor.rowcount,
                0,
            )
            result["deleted_prices"] = max(
                prices_cursor.rowcount,
                0,
            )

            connection.commit()

            page_count = int(
                connection.execute(
                    "PRAGMA page_count"
                ).fetchone()[0]
            )

            free_pages = int(
                connection.execute(
                    "PRAGMA freelist_count"
                ).fetchone()[0]
            )

            free_ratio = (
                free_pages / page_count
                if page_count > 0
                else 0.0
            )

            result["page_count"] = page_count
            result["free_pages"] = free_pages
            result["free_ratio"] = free_ratio

        if (
            run_vacuum
            and result["free_ratio"]
            >= vacuum_min_free_ratio
        ):
            # VACUUM выполняется вне предыдущей транзакции.
            with closing(get_connection()) as connection:
                connection.execute(
                    "PRAGMA busy_timeout = 60000"
                )
                connection.execute("VACUUM")

            result["vacuum_performed"] = True

        logger.info(
            (
                "Обслуживание базы завершено: "
                "snapshots удалено=%s, "
                "prices удалено=%s, "
                "свободно=%.1f%%, "
                "VACUUM=%s"
            ),
            result["deleted_snapshots"],
            result["deleted_prices"],
            float(result["free_ratio"]) * 100,
            result["vacuum_performed"],
        )

    except sqlite3.OperationalError as error:
        # Если в момент обслуживания база занята сканером,
        # бот продолжит работу и повторит очистку позже.
        logger.warning(
            "Обслуживание базы временно пропущено: %s",
            error,
        )

    except Exception:
        logger.exception(
            "Ошибка автоматического обслуживания базы"
        )

    return result

def group_alert_on_cooldown(
    group_key: str,
    alert_family: str,
    cooldown_hours: float,
) -> bool:
    if not group_key:
        return False

    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(cooldown_hours))
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT created_at
            FROM market_group_alerts
            WHERE group_key = ? AND alert_family = ?
            """,
            (str(group_key), str(alert_family)),
        ).fetchone()

    if row is None:
        return False

    try:
        created_at = datetime.fromisoformat(str(row[0]))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False

    return created_at >= cutoff


def save_group_alert(
    group_key: str,
    alert_family: str,
    market_id: str,
) -> None:
    if not group_key:
        return

    now = datetime.now(timezone.utc).isoformat()
    with closing(get_connection()) as connection:
        connection.execute(
            """
            INSERT INTO market_group_alerts (
                group_key, alert_family, market_id, created_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(group_key, alert_family) DO UPDATE SET
                market_id = excluded.market_id,
                created_at = excluded.created_at
            """,
            (str(group_key), str(alert_family), str(market_id), now),
        )
        connection.commit()


def get_smart_cooldown_records(
    identities: list[tuple[str, str]],
    signal_family: str,
) -> list[dict[str, object]]:
    if not identities:
        return []

    clauses = " OR ".join(
        "(identity_type = ? AND identity_key = ?)" for _ in identities
    )
    parameters: list[object] = [str(signal_family)]
    for identity_type, identity_key in identities:
        parameters.extend((str(identity_type), str(identity_key)))

    query = f"""
        SELECT identity_type, identity_key, signal_family, market_id,
               event_id, price, title, created_at
        FROM smart_cooldowns
        WHERE signal_family = ?
          AND ({clauses})
    """

    with closing(get_connection()) as connection:
        rows = connection.execute(query, parameters).fetchall()

    columns = (
        "identity_type", "identity_key", "signal_family", "market_id",
        "event_id", "price", "title", "created_at",
    )
    return [dict(zip(columns, row)) for row in rows]


def save_smart_cooldown_records(
    *,
    identities: list[tuple[str, str]],
    signal_family: str,
    market_id: str,
    event_id: str,
    price: float,
    title: str,
) -> None:
    if not identities:
        return

    created_at = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            str(identity_type),
            str(identity_key),
            str(signal_family),
            str(market_id),
            str(event_id),
            float(price or 0.0),
            str(title),
            created_at,
        )
        for identity_type, identity_key in identities
    ]

    with closing(get_connection()) as connection:
        connection.executemany(
            """
            INSERT INTO smart_cooldowns (
                identity_type, identity_key, signal_family, market_id,
                event_id, price, title, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity_type, identity_key, signal_family) DO UPDATE SET
                market_id = excluded.market_id,
                event_id = excluded.event_id,
                price = excluded.price,
                title = excluded.title,
                created_at = excluded.created_at
            """,
            rows,
        )
        connection.commit()



def record_smart_cooldown_event(
    *,
    decision: str,
    category: str,
    reason: str,
    identity_type: str,
    signal_family: str,
    market_id: str,
    event_id: str,
    title: str,
    price: float,
    previous_price: Optional[float] = None,
    price_move_percent: Optional[float] = None,
    remaining_hours: Optional[float] = None,
) -> None:
    """Write one Smart Cooldown decision to the analytics journal."""
    now = datetime.now(timezone.utc).isoformat()
    with closing(get_connection()) as connection:
        connection.execute(
            """
            INSERT INTO smart_cooldown_events (
                decision, category, reason, identity_type, signal_family,
                market_id, event_id, title, price, previous_price,
                price_move_percent, remaining_hours, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(decision),
                str(category),
                str(reason),
                str(identity_type),
                str(signal_family),
                str(market_id),
                str(event_id),
                str(title),
                float(price or 0.0),
                None if previous_price is None else float(previous_price),
                None if price_move_percent is None else float(price_move_percent),
                None if remaining_hours is None else float(remaining_hours),
                now,
            ),
        )
        connection.commit()


def get_smart_cooldown_stats(hours: int = 24) -> dict[str, object]:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))
    ).isoformat()
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT decision, category, COUNT(*)
            FROM smart_cooldown_events
            WHERE created_at >= ?
            GROUP BY decision, category
            """,
            (cutoff,),
        ).fetchall()

    blocked = 0
    allowed = 0
    categories = {
        "market_id": 0,
        "event_group": 0,
        "question": 0,
        "opportunity": 0,
        "other": 0,
    }
    for decision, category, count in rows:
        count = int(count or 0)
        if str(decision) == "blocked":
            blocked += count
            key = str(category or "other")
            categories[key if key in categories else "other"] += count
        elif str(decision) == "allowed_repeat":
            allowed += count

    repeat_checks = blocked + allowed
    reduction_rate = (blocked / repeat_checks * 100.0) if repeat_checks else None
    return {
        "hours": int(hours),
        "blocked": blocked,
        "allowed_repeats": allowed,
        "repeat_checks": repeat_checks,
        "reduction_rate": reduction_rate,
        "categories": categories,
    }


def get_recent_smart_cooldown_events(
    hours: int = 24,
    limit: int = 10,
) -> list[dict[str, object]]:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))
    ).isoformat()
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT decision, category, reason, identity_type, signal_family,
                   market_id, event_id, title, price, previous_price,
                   price_move_percent, remaining_hours, created_at
            FROM smart_cooldown_events
            WHERE created_at >= ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (cutoff, max(1, int(limit))),
        ).fetchall()

    columns = (
        "decision", "category", "reason", "identity_type", "signal_family",
        "market_id", "event_id", "title", "price", "previous_price",
        "price_move_percent", "remaining_hours", "created_at",
    )
    return [dict(zip(columns, row)) for row in rows]
