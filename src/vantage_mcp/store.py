"""API key issuance + usage metering, backed by local SQLite.

Deliberately not Postgres/Supabase yet: this is a pilot testing whether
there's any demand at all (see the stress-test finding: under 5% of MCP
servers monetize). SQLite costs nothing extra to stand up and is a
5-minute migration to Postgres later if real usage ever shows up -
swap this module's internals, keep the same three functions' contracts.
"""

import hashlib
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "vantage.db"

# Calls per billing period (calendar month), matched to the pricing
# discussed in the pilot plan. DataForSEO COGS is ~$0.10-0.15/call for
# the two expensive tools, so the free tier is intentionally thin -
# a generous free tier here loses money on day one.
TIER_LIMITS = {
    "free": 3,
    "pro": 50,
    "team": 150,
}


def _migrate(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS is a no-op against a table that already
    exists with an older shape - caught live in test-mode Stripe testing,
    where a pre-existing vantage.db (from earlier manual key-issuance
    testing) silently kept its old schema and every real column-access
    threw. Add any columns a prior version of this file didn't have yet."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(api_keys)")}
    for column, ddl_type in (
        ("stripe_customer_id", "TEXT"),
        ("stripe_subscription_id", "TEXT"),
        ("email", "TEXT"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE api_keys ADD COLUMN {column} {ddl_type}")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS api_keys (
            key_hash TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            tier TEXT NOT NULL,
            created_at TEXT NOT NULL,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT
        )"""
    )
    _migrate(conn)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_stripe_customer "
        "ON api_keys(stripe_customer_id) WHERE stripe_customer_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_email "
        "ON api_keys(email) WHERE email IS NOT NULL"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS usage (
            client_id TEXT NOT NULL,
            period TEXT NOT NULL,
            calls_used INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (client_id, period)
        )"""
    )
    return conn


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _current_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def create_api_key(
    tier: str = "free",
    stripe_customer_id: str | None = None,
    stripe_subscription_id: str | None = None,
    email: str | None = None,
) -> tuple[str, str]:
    """Issue a new key. Returns (plaintext_key, client_id) - the
    plaintext is shown ONCE, only the hash is ever stored. Manual
    CLI issuance omits every optional arg; the billing service supplies
    stripe_* for a paid key or email for a free self-serve signup.
    """
    if tier not in TIER_LIMITS:
        raise ValueError(f"unknown tier {tier!r}, must be one of {list(TIER_LIMITS)}")
    plaintext = "vtg_" + secrets.token_urlsafe(32)
    client_id = "cli_" + secrets.token_hex(6)
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO api_keys (key_hash, client_id, tier, created_at, stripe_customer_id, stripe_subscription_id, email) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _hash(plaintext),
                client_id,
                tier,
                datetime.now(timezone.utc).isoformat(),
                stripe_customer_id,
                stripe_subscription_id,
                email,
            ),
        )
        conn.commit()
    return plaintext, client_id


def get_key_by_stripe_customer(stripe_customer_id: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT client_id, tier FROM api_keys WHERE stripe_customer_id = ?", (stripe_customer_id,)
        ).fetchone()
    return {"client_id": row[0], "tier": row[1]} if row else None


def create_api_key_for_email(email: str) -> tuple[str | None, str]:
    """Idempotent get-or-create for a free-tier self-serve signup, keyed
    by email instead of a Stripe customer (no payment involved). Same
    idempotency contract as the Stripe path: returns (None, client_id)
    if this email already has a key, never issues a second one."""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT client_id FROM api_keys WHERE email = ?", (email,)).fetchone()
    if row:
        return None, row[0]
    return create_api_key(tier="free", email=email)


def create_api_key_for_stripe_customer(
    stripe_customer_id: str, stripe_subscription_id: str, tier: str
) -> tuple[str | None, str]:
    """Idempotent get-or-create for a paying Stripe customer. Both the
    checkout-success page and the webhook call this independently (the
    webhook can arrive before or after the customer's browser redirect),
    so this must never issue two keys for the same customer.

    Returns (plaintext_or_None, client_id). plaintext is None if a key
    already existed - it was already shown once and is never re-shown.
    """
    existing = get_key_by_stripe_customer(stripe_customer_id)
    if existing:
        return None, existing["client_id"]
    return create_api_key(tier=tier, stripe_customer_id=stripe_customer_id, stripe_subscription_id=stripe_subscription_id)


def update_tier_for_stripe_customer(stripe_customer_id: str, new_tier: str) -> bool:
    """Subscription upgraded/downgraded. Returns True if a matching key was found."""
    if new_tier not in TIER_LIMITS:
        raise ValueError(f"unknown tier {new_tier!r}, must be one of {list(TIER_LIMITS)}")
    with closing(_connect()) as conn:
        cur = conn.execute(
            "UPDATE api_keys SET tier = ? WHERE stripe_customer_id = ?", (new_tier, stripe_customer_id)
        )
        conn.commit()
        return cur.rowcount > 0


def deactivate_stripe_customer(stripe_customer_id: str, downgrade_to: str = "free") -> bool:
    """Subscription cancelled - downgrade rather than hard-delete, so a
    lapsed customer isn't locked out entirely, just loses paid capacity.
    Returns True if a matching key was found."""
    return update_tier_for_stripe_customer(stripe_customer_id, downgrade_to)


def verify(token: str) -> dict | None:
    """Look up a presented bearer token. Returns {"client_id", "tier"} or None."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT client_id, tier FROM api_keys WHERE key_hash = ?", (_hash(token),)
        ).fetchone()
    if not row:
        return None
    return {"client_id": row[0], "tier": row[1]}


def check_and_consume(client_id: str, tier: str) -> tuple[bool, int, str | None]:
    """Check this billing period's usage against the tier cap, and
    consume one call if under it (checked BEFORE the paid DataForSEO
    call runs, so a rejected call never costs us anything). Returns
    (allowed, calls_remaining_after, reason_if_denied).
    """
    limit = TIER_LIMITS.get(tier, 0)
    period = _current_period()
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT calls_used FROM usage WHERE client_id = ? AND period = ?", (client_id, period)
        ).fetchone()
        used = row[0] if row else 0

        if used >= limit:
            return False, 0, (
                f"Usage cap reached for this billing period ({used}/{limit} calls on the "
                f"'{tier}' tier). Upgrade at vantagemcp.dev/pricing, or wait for next period."
            )

        conn.execute(
            "INSERT INTO usage (client_id, period, calls_used) VALUES (?, ?, 1) "
            "ON CONFLICT(client_id, period) DO UPDATE SET calls_used = calls_used + 1",
            (client_id, period),
        )
        conn.commit()
    return True, limit - used - 1, None
