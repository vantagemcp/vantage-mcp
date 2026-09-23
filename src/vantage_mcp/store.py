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
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "vantage.db"

# Cost-units per billing period (calendar month), not raw call counts.
# find_citation_leaders costs 10 units/call (DataForSEO
# COGS ~$0.10-0.15/call), analyze_citation_structure(_batch),
# analyze_citation_trend, and analyze_citation_gap cost 1 unit/call
# (COGS ~$0.004-0.007/call). Limits are the old call-based limits x10, so
# worst-case cost exposure (all-expensive-calls) is unchanged from the
# original per-tier caps - a generous free tier still doesn't lose
# money on day one - but a user who only wants the cheap, differentiated
# structure tool gets a genuinely generous free experience instead of
# being capped the same as the expensive tools.
TIER_LIMITS = {
    "free": 30,
    "pro": 500,
    "team": 1500,
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
    # Each key's coverage results, so the next check of the same domain and
    # keyword can say what changed (the "did that work?" half of an agent's
    # loop). Kept CHECK_RETENTION_DAYS, never shared across keys.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS checks (
            client_id TEXT NOT NULL,
            domain TEXT NOT NULL,
            keyword TEXT NOT NULL,
            engine TEXT NOT NULL,
            country TEXT NOT NULL,
            language TEXT NOT NULL,
            samples INTEGER NOT NULL,
            cited_runs INTEGER NOT NULL,
            best_rank INTEGER,
            mentioned_runs INTEGER NOT NULL,
            checked_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_checks_lookup ON checks(client_id, domain, keyword, checked_at)"
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

    This path deliberately records no email. Whatever a deployment wants
    to keep against a paying customer is that deployment's own billing
    concern and is not part of this package; the free self-serve path
    above (create_api_key_for_email) is the one that keys on email.

    Returns (plaintext_or_None, client_id). plaintext is None if a key
    already existed - it was already shown once and is never re-shown.
    """
    existing = get_key_by_stripe_customer(stripe_customer_id)
    if existing:
        return None, existing["client_id"]
    return create_api_key(
        tier=tier, stripe_customer_id=stripe_customer_id, stripe_subscription_id=stripe_subscription_id
    )


def update_tier_for_stripe_customer(stripe_customer_id: str, new_tier: str) -> bool:
    """Subscription upgraded/downgraded. Returns True if a matching key was found.

    Updates every key of the account, not only the row carrying the Stripe
    id: since 1.8.0 an account can hold extra keys issued through OAuth
    sign-in (add_key_for_client), and those rows have no stripe_customer_id,
    so matching on it alone left them on the old tier after an upgrade."""
    if new_tier not in TIER_LIMITS:
        raise ValueError(f"unknown tier {new_tier!r}, must be one of {list(TIER_LIMITS)}")
    with closing(_connect()) as conn:
        cur = conn.execute(
            "UPDATE api_keys SET tier = ? WHERE client_id IN "
            "(SELECT client_id FROM api_keys WHERE stripe_customer_id = ?)",
            (new_tier, stripe_customer_id),
        )
        conn.commit()
        return cur.rowcount > 0


def add_key_for_client(client_id: str) -> str | None:
    """Issue one more key for an existing account, on its current tier, and
    return the plaintext (shown once, only the hash is stored), or None if
    the account does not exist. Used by OAuth sign-in: each connected client
    gets its own key, so disconnecting one never breaks another. The extra
    row carries no email or Stripe id, so signup counts, which key off
    `email IS NOT NULL`, still count the account once."""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT tier FROM api_keys WHERE client_id = ? LIMIT 1", (client_id,)).fetchone()
        if not row:
            return None
        plaintext = "vtg_" + secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO api_keys (key_hash, client_id, tier, created_at) VALUES (?, ?, ?, ?)",
            (_hash(plaintext), client_id, row[0], datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    return plaintext


# ---- OAuth sign-in storage (1.8.0) ----
# JSON blobs of the SDK's own pydantic models, keyed by their ids. Pending
# requests and codes are short-lived and single use.

def _oauth_tables(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS oauth_clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL, created_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS oauth_pending (request_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL, expires_at REAL NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS oauth_codes (code TEXT PRIMARY KEY, info TEXT NOT NULL, expires_at REAL NOT NULL)")


def oauth_put(table: str, key: str, value: str, expires_at: float | None = None) -> None:
    with closing(_connect()) as conn:
        _oauth_tables(conn)
        if table == "oauth_clients":
            conn.execute("INSERT OR REPLACE INTO oauth_clients VALUES (?, ?, ?)",
                         (key, value, datetime.now(timezone.utc).isoformat()))
        elif table == "oauth_codes":
            conn.execute("INSERT INTO oauth_codes VALUES (?, ?, ?)", (key, value, expires_at))
        else:
            raise ValueError(table)
        conn.commit()


def oauth_get_client(client_id: str) -> str | None:
    with closing(_connect()) as conn:
        _oauth_tables(conn)
        row = conn.execute("SELECT info FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
    return row[0] if row else None


def oauth_put_pending(request_id: str, client_id: str, params: str, expires_at: float) -> None:
    with closing(_connect()) as conn:
        _oauth_tables(conn)
        conn.execute("DELETE FROM oauth_pending WHERE expires_at < ?", (datetime.now(timezone.utc).timestamp(),))
        conn.execute("INSERT INTO oauth_pending VALUES (?, ?, ?, ?)", (request_id, client_id, params, expires_at))
        conn.commit()


def oauth_peek_pending(request_id: str) -> tuple[str, str] | None:
    """(client_id, params_json) for a live pending request, without using it up."""
    with closing(_connect()) as conn:
        _oauth_tables(conn)
        row = conn.execute("SELECT client_id, params, expires_at FROM oauth_pending WHERE request_id = ?",
                           (request_id,)).fetchone()
    if not row or row[2] < datetime.now(timezone.utc).timestamp():
        return None
    return row[0], row[1]


def oauth_take_pending(request_id: str) -> tuple[str, str] | None:
    """(client_id, params_json), deleting the request in the same step so a
    consent can only ever be completed once."""
    with closing(_connect()) as conn:
        _oauth_tables(conn)
        row = conn.execute("SELECT client_id, params, expires_at FROM oauth_pending WHERE request_id = ?",
                           (request_id,)).fetchone()
        conn.execute("DELETE FROM oauth_pending WHERE request_id = ?", (request_id,))
        conn.commit()
    if not row or row[2] < datetime.now(timezone.utc).timestamp():
        return None
    return row[0], row[1]


def oauth_take_code(code: str) -> str | None:
    """A code's JSON, deleted in the same step so it can be exchanged once."""
    with closing(_connect()) as conn:
        _oauth_tables(conn)
        row = conn.execute("SELECT info, expires_at FROM oauth_codes WHERE code = ?", (code,)).fetchone()
        conn.execute("DELETE FROM oauth_codes WHERE code = ?", (code,))
        conn.commit()
    if not row or row[1] < datetime.now(timezone.utc).timestamp():
        return None
    return row[0]


def oauth_peek_code(code: str) -> str | None:
    with closing(_connect()) as conn:
        _oauth_tables(conn)
        row = conn.execute("SELECT info, expires_at FROM oauth_codes WHERE code = ?", (code,)).fetchone()
    if not row or row[1] < datetime.now(timezone.utc).timestamp():
        return None
    return row[0]


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


def usage_status(client_id: str, tier: str) -> dict:
    """Read-only usage snapshot for this billing period. Never consumes
    anything - added alongside the get_usage tool, itself added because there
    is no dashboard anywhere for Vantage: the only way an agent (or the
    customer through it) could previously learn the cap existed at all was to
    hit it mid-workflow and get denied. Same period/limit logic as
    check_and_consume, deliberately kept as a straight read."""
    limit = TIER_LIMITS.get(tier, 0)
    period = _current_period()
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT calls_used FROM usage WHERE client_id = ? AND period = ?", (client_id, period)
        ).fetchone()
    used = row[0] if row else 0
    return {"tier": tier, "period": period, "units_used": used, "units_limit": limit,
            "units_remaining": max(limit - used, 0)}


def check_and_consume(client_id: str, tier: str, cost: int) -> tuple[bool, int, str | None]:
    """Check this billing period's usage against the tier cap, and
    consume `cost` units if there's room (checked BEFORE the paid
    DataForSEO call runs, so a rejected call never costs us anything).
    `cost` is required, not defaulted, so every caller states its own
    tool's weight explicitly rather than silently inheriting a wrong one.
    Returns (allowed, units_remaining_after, reason_if_denied).
    """
    limit = TIER_LIMITS.get(tier, 0)
    period = _current_period()
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT calls_used FROM usage WHERE client_id = ? AND period = ?", (client_id, period)
        ).fetchone()
        used = row[0] if row else 0

        if used + cost > limit:
            return False, max(limit - used, 0), (
                f"Usage cap reached for this billing period ({used}/{limit} units on the "
                f"'{tier}' tier, this call needs {cost}). Upgrade at vantagemcp.dev/pricing, "
                "or wait for next period."
            )

        conn.execute(
            "INSERT INTO usage (client_id, period, calls_used) VALUES (?, ?, ?) "
            "ON CONFLICT(client_id, period) DO UPDATE SET calls_used = calls_used + ?",
            (client_id, period, cost, cost),
        )
        conn.commit()
    return True, limit - used - cost, None


CHECK_RETENTION_DAYS = 180
_CHECK_COLS = ("domain", "keyword", "engine", "country", "language", "samples", "cited_runs",
               "best_rank", "mentioned_runs", "checked_at")


def record_check(client_id: str, domain: str, keyword: str, engine: str, country: str,
                 language: str, samples: int, cited_runs: int, best_rank: int | None,
                 mentioned_runs: int) -> dict | None:
    """Save one keyword's coverage result and return the previous result for
    the same key, domain, keyword, engine and market (or None on a first
    check). Also drops this key's rows past CHECK_RETENTION_DAYS, so the
    retention the privacy page promises is enforced by the write path itself."""
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=CHECK_RETENTION_DAYS)).isoformat()
    with closing(_connect()) as conn:
        row = conn.execute(
            f"SELECT {', '.join(_CHECK_COLS)} FROM checks WHERE client_id = ? AND domain = ? AND "
            "keyword = ? AND engine = ? AND country = ? AND language = ? "
            "ORDER BY checked_at DESC LIMIT 1",
            (client_id, domain, keyword, engine, country, language),
        ).fetchone()
        conn.execute("DELETE FROM checks WHERE client_id = ? AND checked_at < ?", (client_id, cutoff))
        conn.execute(
            f"INSERT INTO checks (client_id, {', '.join(_CHECK_COLS)}) VALUES (?{', ?' * len(_CHECK_COLS)})",
            (client_id, domain, keyword, engine, country, language, samples, cited_runs,
             best_rank, mentioned_runs, now.isoformat()),
        )
        conn.commit()
    return dict(zip(_CHECK_COLS, row)) if row else None


def check_history(client_id: str, domain: str, keyword: str | None = None, limit: int = 50) -> list[dict]:
    """This key's saved results for a domain, newest first."""
    sql = f"SELECT {', '.join(_CHECK_COLS)} FROM checks WHERE client_id = ? AND domain = ?"
    args: list = [client_id, domain]
    if keyword:
        sql += " AND keyword = ?"
        args.append(keyword)
    sql += " ORDER BY checked_at DESC LIMIT ?"
    args.append(limit)
    with closing(_connect()) as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(zip(_CHECK_COLS, r)) for r in rows]


def recent_checks(client_id: str, limit: int = 25) -> list[dict]:
    """This key's saved results across every domain, newest first (the
    account page)."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_CHECK_COLS)} FROM checks WHERE client_id = ? ORDER BY checked_at DESC LIMIT ?",
            (client_id, limit)).fetchall()
    return [dict(zip(_CHECK_COLS, r)) for r in rows]


def refund(client_id: str, cost: int) -> None:
    """Hand back `cost` units charged by check_and_consume for a call that
    turned out not to deliver a usable result.

    NOT a general safety net: _guard_balance is checked before _guard_usage
    now precisely so the common failure (provider unreachable / our own
    balance too low) never charges anything in the first place. This exists
    for the narrower case that ordering can't prevent - the balance check
    passes, the DataForSEO call is made, and IT still comes back unusable
    (a malformed response, an exception during parsing) - so quota already
    spent on nothing gets returned rather than silently kept. Floored at 0:
    a customer who somehow gets refunded more than they used should end up
    at 0 for the period, not negative (which would look like a bonus)."""
    period = _current_period()
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE usage SET calls_used = MAX(0, calls_used - ?) "
            "WHERE client_id = ? AND period = ?",
            (cost, client_id, period),
        )
        conn.commit()
