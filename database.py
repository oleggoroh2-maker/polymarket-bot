import os
import sqlite3
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
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        connection.commit()


def save_price(market_id: str, price: float) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()

    with closing(get_connection()) as connection:
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
                float(price),
                timestamp,
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


def cleanup_prices(days: int = 30) -> None:
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


# Совместимость со старым scanner.py
def cleanup(days: int = 30) -> None:
    cleanup_prices(days)


if __name__ == "__main__":
    init_db()
    cleanup_prices()
    cleanup_alerts()

    print(f"✅ База данных готова: {DB_PATH}")