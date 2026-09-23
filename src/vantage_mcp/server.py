"""Vantage: check how visible a brand/domain is inside AI answer
engines (ChatGPT, Google AI Overview) directly from an agent session.

Run locally over stdio for testing (unauthenticated - trusted local
dev, metering is skipped entirely in this mode):
    python -m vantage_mcp.server

Run as a hosted Streamable HTTP endpoint (what registries need, and
where API-key auth + usage metering actually apply):
    python -m vantage_mcp.server --http
"""

import html
import json
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from vantage_mcp import dataforseo_client as dfs
from vantage_mcp import store
from vantage_mcp.auth import SCOPE as OAUTH_SCOPE
from vantage_mcp.auth import VantageOAuthProvider, complete_consent, deny_consent, tier_from_scopes

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SUPPORT_EMAIL = "support@vantagemcp.dev"
CALL_LOG_PATH = "/var/log/vantage/calls.jsonl"

MIN_BALANCE_USD = 1.0  # same hard-stop guardrail as the source pipeline
BASE_URL = "https://vantagemcp.dev"
DOMAIN = "vantagemcp.dev"

# Every tool below shares this exact profile: pure reads against a
# third-party data provider, no writes, safe to retry, results depend
# on external (non-deterministic over time) data. One shared constant
# so each @mcp.tool() call doesn't repeat identical annotation blocks.
READ_ONLY_EXTERNAL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

# DataForSEO's llm_mentions endpoint family (what find_citation_leaders
# and analyze_citation_trend call) only ever supports these two - confirmed
# directly against their API docs for target_metrics/live and top_domains/
# live. Perplexity and Gemini were never real: this project's own docs
# and descriptions claimed them from day one, but DataForSEO would either
# error or silently mishandle the request - nobody had actually hit it
# yet (checked the real call logs), but it was only a matter of time.
VALID_PLATFORMS = {"chat_gpt", "google"}


def _guard_platform(platform: str) -> str | None:
    if platform not in VALID_PLATFORMS:
        return (
            f'"{platform}" is not a supported platform. This check only '
            'covers "chat_gpt" and "google" (Google\'s AI Overview) right '
            "now - Perplexity and Gemini aren't available here."
        )
    return None


def _market(country: str | None, language: str | None) -> tuple[str, str]:
    """A caller's country/language, blanks falling back to the default market.
    Values are passed to the provider as-is; one it does not support comes
    back as a provider error, which each tool already refunds and reports."""
    return ((country or "").strip() or dfs.DEFAULT_COUNTRY,
            (language or "").strip().lower() or dfs.DEFAULT_LANGUAGE)


def _guard_mentions_market(platform: str, country: str, language: str) -> str | None:
    """The llm_mentions data behind find_citation_leaders and
    analyze_citation_trend only covers ChatGPT in the United States, in
    English (DataForSEO docs, checked 2026-09-24); Google's AI Overview data
    covers more markets. Refused here, before any quota is spent."""
    if platform == "chat_gpt" and (country.lower(), language) != (dfs.DEFAULT_COUNTRY.lower(), dfs.DEFAULT_LANGUAGE):
        return (
            'ChatGPT data for this check only exists for country "United States", language "en". '
            'Use platform "google" (Google\'s AI Overview) for other markets, or '
            "check_prompt_coverage / analyze_citation_structure, which read a live ChatGPT "
            "answer in the market you choose."
        )
    return None

# The SDK's DNS-rebinding protection validates the Host/Origin headers
# against an explicit allowlist - leaving it unconfigured meant an empty
# allowed_hosts list, which silently rejects every real request with a
# 421 (caught live testing the public deploy, not in local dev, since
# local stdio/loopback traffic never exercises this check).
TRANSPORT_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[DOMAIN, f"{DOMAIN}:443", "127.0.0.1:8420", "localhost:8420"],
    allowed_origins=[f"https://{DOMAIN}"],
)

OAUTH_PROVIDER = VantageOAuthProvider(BASE_URL)

mcp = MCPServer(
    name="vantage",
    instructions=(
        "Checks whether a brand or domain is cited inside AI answer engines "
        "(ChatGPT, Gemini, Perplexity, Google AI Overview), which questions "
        "already cite it, how that's changed over time, and how the winning "
        "AI-generated answer for a given topic is structured. Use this when a "
        "user asks things like 'does ChatGPT know about my product', 'what does "
        "AI cite us for', 'who gets cited for this keyword in AI search', 'did "
        "our changes work', or 'what does a winning AI answer look like for X'. "
        "Answers vary between runs: use samples=3 before telling someone they "
        "are or are not cited."
    ),
    # OAuth sign-in (1.8.0). The provider also verifies plain API keys, so
    # every key issued before keeps working exactly as it did.
    auth_server_provider=OAUTH_PROVIDER,
    auth=AuthSettings(
        issuer_url=BASE_URL,
        resource_server_url=f"{BASE_URL}/mcp",
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[OAUTH_SCOPE], default_scopes=[OAUTH_SCOPE]),
    ),
)


_CONSENT_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Connect to Vantage</title>
<style>
:root {{ color-scheme: dark; --bg: #0a0f0f; --panel: #0f1717; --ink: #eef3f2; --muted: #9aa9a7;
  --line: #22403c; --primary: #3fd1a6; --err: #ff8a80; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
main {{ max-width: 30rem; margin: 0 auto; padding: 3rem 1rem; }}
h1 {{ font-size: 1.6rem; margin: 0 0 .5rem; }}
p {{ color: var(--muted); margin: 0 0 1rem; }}
form {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  padding: 1.25rem; margin: 0 0 1rem; }}
label {{ display: block; font-weight: 600; margin: 0 0 .4rem; }}
input {{ width: 100%; padding: .7rem .8rem; border-radius: 8px; border: 1px solid var(--line);
  background: var(--bg); color: var(--ink); font: inherit; }}
input:focus-visible, button:focus-visible {{ outline: 2px solid var(--primary); outline-offset: 2px; }}
button, .btn {{ display: block; text-align: center; text-decoration: none; margin-top: .8rem;
  width: 100%; padding: .7rem; border: 0; border-radius: 8px; background: var(--primary);
  color: #04211a; font: inherit; font-weight: 700; cursor: pointer; }}
.btn:focus-visible {{ outline: 2px solid var(--primary); outline-offset: 2px; }}
.ghost {{ background: transparent; color: var(--ink); border: 1px solid var(--line); }}
form.plain {{ background: none; border: 0; padding: 0; }}
.link {{ background: none; color: var(--muted); text-decoration: underline; margin-top: 0; }}
.err {{ color: var(--err); }}
code {{ display: block; word-break: break-all; background: var(--bg); border: 1px solid var(--line);
  border-radius: 8px; padding: .7rem; margin: .5rem 0 1rem; color: var(--ink); }}
a {{ color: var(--primary); }}
</style></head><body><main>{body}</main></body></html>"""


def _consent_html(body: str, status: int = 200):
    from starlette.responses import HTMLResponse
    return HTMLResponse(_CONSENT_PAGE.format(body=body), status_code=status,
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


def _consent_form(req: str, client_name: str, error: str = "") -> str:
    esc = html.escape
    return f"""<h1>Connect {esc(client_name)} to Vantage</h1>
<p>{esc(client_name)} is asking to run Vantage's AI-citation checks for you. It will use your
Vantage allowance (30 free units a month on the free plan).</p>
{f'<p class="err" role="alert">{esc(error)}</p>' if error else ''}
<form method="post">
  <input type="hidden" name="req" value="{esc(req)}">
  <label for="email">New to Vantage? Your email</label>
  <input id="email" name="email" type="email" autocomplete="email" placeholder="you@example.com">
  <button type="submit" name="action" value="email">Create a free account and connect</button>
</form>
<form method="post">
  <input type="hidden" name="req" value="{esc(req)}">
  <label for="key">Already have a Vantage API key?</label>
  <input id="key" name="key" type="password" autocomplete="off" placeholder="vtg_...">
  <button class="ghost" type="submit" name="action" value="key">Sign in with this key and connect</button>
</form>
<form class="plain" method="post">
  <input type="hidden" name="req" value="{esc(req)}">
  <button class="link" type="submit" name="action" value="deny">Cancel and go back</button>
</form>
<p>No card needed. <a href="{BASE_URL}/legal/">Privacy and terms</a>.</p>"""


@mcp.custom_route("/oauth/consent", methods=["GET", "POST"])
async def oauth_consent(request):
    """The page an MCP client sends a person to during OAuth sign-in: create
    a free account by email, or sign in with an existing API key. See auth.py
    for why an existing email cannot sign in by itself."""
    from starlette.responses import RedirectResponse

    if request.method == "GET":
        req = request.query_params.get("req", "")
    else:
        form = await request.form()
        req = str(form.get("req") or "")
    pending = store.oauth_peek_pending(req) if req else None
    if not pending:
        return _consent_html("<h1>This sign-in link has expired</h1><p>Go back to your app and "
                             "start connecting Vantage again.</p>", 400)
    client = await OAUTH_PROVIDER.get_client(pending[0])
    client_name = (client.client_name if client and client.client_name else "An app")[:60]
    if request.method == "GET":
        return _consent_html(_consent_form(req, client_name))

    action = form.get("action")
    if action == "deny":
        return RedirectResponse(deny_consent(req) or BASE_URL, status_code=302)
    if action == "key":
        record = store.verify(str(form.get("key") or "").strip())
        if not record:
            return _consent_html(_consent_form(req, client_name, "That key was not recognised."), 400)
        _log_oauth("oauth_key_signin", record["client_id"])
        return RedirectResponse(complete_consent(req, record["client_id"]) or BASE_URL, status_code=302)
    email = str(form.get("email") or "").strip()
    if not EMAIL_RE.match(email):
        return _consent_html(_consent_form(req, client_name, "Enter a valid email address."), 400)
    plaintext, account = store.create_api_key_for_email(email)
    if not plaintext:
        return _consent_html(_consent_form(
            req, client_name, "That email already has a Vantage account. Sign in with its API key "
            f"below, or email {SUPPORT_EMAIL} if you have lost it."), 400)
    _log_oauth("oauth_email_signup", account)
    target = complete_consent(req, account) or BASE_URL
    esc = html.escape
    return _consent_html(f"""<h1>You're connected</h1>
<p>Your free Vantage account is ready and {esc(client_name)} is connected. This is your account's
API key for any other tool, shown once, so save it now:</p>
<code>{esc(plaintext)}</code>
<a class="btn" href="{esc(target)}">Continue to {esc(client_name)}</a>""")


def _log_oauth(event: str, client_id: str) -> None:
    line = json.dumps({"ts": time.time(), "client_id": client_id, "tool": event, "outcome": "success"})
    print(line, flush=True)
    try:
        with open(CALL_LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass




def _log_call(tool: str, outcome: str, **extra: object) -> None:
    """One structured line per real (metered) tool call, so it's answerable
    later whether a given signup ever had a working call - not just whether
    a key was issued. Captured automatically by systemd/journald, same as
    every other log line this process already emits, and mirrored to a
    plain file so the tool-health check can read recent outcomes without
    journal-read permissions. Only logs when there's a real access token
    (hosted transport, real customer) - stdio/local-dev calls are unmetered
    and untracked, same scope as the usage guard.
    """
    token = get_access_token()
    if token is None:
        return
    line = json.dumps({
        "ts": time.time(),
        "client_id": token.client_id,
        "tool": tool,
        "outcome": outcome,
        **extra,
    })
    print(line, flush=True)
    try:
        with open(CALL_LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass  # the journald copy above is authoritative; this is a convenience mirror


# DataForSEO's balance endpoint allows 6 requests a minute per ACCOUNT
# (shared with every other job on that account), and every metered call used
# to hit it first, so a run of checks got refused for no reason: 3 of 30 in
# an internal test run of 2026-09-11. The last good reading is reused for
# BALANCE_FRESH_S; a failed lookup falls back to it only while it is younger
# than BALANCE_FALLBACK_S, and the $1 floor still applies to it. Past that it
# fails closed, exactly as before. Worst case is about 15 minutes of checks
# after the balance really drops below the floor, which is cents.
BALANCE_FRESH_S = 300
BALANCE_FALLBACK_S = 900
_balance_cache: dict = {"value": None, "at": 0.0}


def _guard_balance() -> str | None:
    now = time.monotonic()
    cached, at = _balance_cache["value"], _balance_cache["at"]
    if cached is not None and now - at < BALANCE_FRESH_S:
        balance = cached
    else:
        try:
            balance = dfs.read_balance()
            _balance_cache["value"], _balance_cache["at"] = balance, now
        except dfs.DataForSEOError:
            if cached is None or now - at >= BALANCE_FALLBACK_S:
                return (
                    "Visibility data provider temporarily unavailable. Try again in a "
                    "few minutes - if it persists, contact support@vantagemcp.dev."
                )
            balance = cached
    if balance < MIN_BALANCE_USD:
        # Deliberately doesn't include the actual balance figure in a
        # user-facing message - that's internal operational state, not
        # something the caller needs to see to know what to do next.
        return (
            "Visibility check temporarily unavailable. Try again shortly - "
            "if it persists, contact support@vantagemcp.dev."
        )
    return None


def _guard_usage(cost: int) -> str | None:
    """Metering gate, checked BEFORE any paid DataForSEO call, and only
    AFTER _guard_balance has already confirmed the provider is reachable
    and our own balance is healthy - that ordering is what stops a provider
    outage from charging a customer's quota for nothing. See _refund_usage
    for the remaining case ordering can't prevent: the balance check passes
    but the call still comes back unusable.

    No access token present means stdio/local-dev mode (there's no
    HTTP auth layer to have populated one) - trusted, unmetered. Over
    Streamable HTTP a token is always required, so this always applies
    to real customers. `cost` is the calling tool's unit weight (10 for
    the two expensive DataForSEO calls, 1 for the four cheap structure/
    gap/trend ones) - see TIER_LIMITS in store.py for why.
    """
    token = get_access_token()
    if token is None:
        return None
    tier = tier_from_scopes(token.scopes)
    allowed, _remaining, reason = store.check_and_consume(token.client_id, tier, cost)
    return None if allowed else reason


def _normalize_domain(domain: str) -> str:
    """Lowercase, stripped, no leading "www." - the exact-match key used
    everywhere a domain from a customer is compared against a domain string
    from the provider. Exact match, not substring: a substring check here
    once let "notion.so" match "mynotion.so.example.com"."""
    return (domain or "").strip().lower().removeprefix("www.")


def _refund_usage(cost: int) -> None:
    """Hand back quota _guard_usage already charged for a call that came
    back unusable (a provider error, or a response we could not parse).
    Same no-token-in-dev-mode shape as _guard_usage, so calling this after
    a guard that was itself a no-op is always safe."""
    token = get_access_token()
    if token is not None:
        store.refund(token.client_id, cost)


def _client_id() -> str | None:
    """The calling key's client id, or None in stdio/local-dev mode."""
    token = get_access_token()
    return token.client_id if token else None


def _guard_live(engine: str, samples: int) -> str | None:
    """Arguments of the live-answer tools, checked before any quota is spent."""
    if engine not in dfs.ENGINES:
        return f'"{engine}" is not a supported engine. Use one of: {", ".join(dfs.ENGINES)}.'
    if not isinstance(samples, int) or not 1 <= samples <= dfs.MAX_SAMPLES:
        return f"samples must be a whole number from 1 to {dfs.MAX_SAMPLES}, got {samples!r}."
    return None


TRANSIENT_ERROR = ("Visibility data provider had a transient error on this request. Try again - "
                   "if it keeps failing, contact support@vantagemcp.dev.")


def _run_error(run: dict) -> str:
    """A failed sample's message for the caller: provider transport errors are
    internal detail (status codes, provider wording), so they become the
    generic transient message; the provider's own "no answer" style messages
    are useful and pass through."""
    return TRANSIENT_ERROR if run.get("transient") else run.get("error") or TRANSIENT_ERROR


def _source_frequency(runs: list[dict]) -> list[dict]:
    """How many of the sampled answers cited each domain, most often first."""
    counts: dict[str, int] = {}
    for run in runs:
        for d in run.get("source_domains") or []:
            counts[d] = counts.get(d, 0) + 1
    return [{"domain": d, "runs": n} for d, n in sorted(counts.items(), key=lambda kv: -kv[1])]


def _change(previous: dict | None, cited_runs: int, samples: int) -> str:
    """This check against the last one for the same key, domain, keyword,
    engine and market, compared as a citation rate so a 1-sample check and a
    3-sample check are comparable."""
    if previous is None:
        return "first_check"
    before = previous["cited_runs"] / max(previous["samples"], 1)
    now = cited_runs / max(samples, 1)
    return "same" if now == before else ("up" if now > before else "down")


def _cited_majority(cited_runs: int, samples: int) -> bool:
    return cited_runs > 0 and cited_runs * 2 >= samples


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def get_usage() -> dict:
    """Check how much of this billing period's quota is left, before
    spending any of it. Use this to answer 'how many checks do I have left'
    or to decide whether a batch call will fit before running it.

    Costs 0 quota units - this never touches the paid data provider, it
    only reads Vantage's own record of what has been used.

    Returns: {"tier", "period" (YYYY-MM), "units_used", "units_limit",
    "units_remaining"}. find_citation_leaders and find_cited_questions cost
    10 units/call; analyze_citation_trend, analyze_citation_structure (and
    its batch form, per keyword), check_prompt_coverage (per keyword) and
    analyze_citation_gap cost 1, times `samples` where a tool takes it.
    get_check_history costs 0.

    stdio/local-dev mode (no HTTP access token) has no metering at all -
    this returns tier "unmetered" with no real limit in that case.
    """
    token = get_access_token()
    if token is None:
        return {"tier": "unmetered", "period": None, "units_used": 0,
                "units_limit": None, "units_remaining": None}
    tier = tier_from_scopes(token.scopes)
    _log_call("get_usage", "success")
    return store.usage_status(token.client_id, tier)


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def find_citation_leaders(keyword: str, platform: str = "chat_gpt", compare_domain: str | None = None,
                          country: str = dfs.DEFAULT_COUNTRY, language: str = dfs.DEFAULT_LANGUAGE) -> dict:
    """Find which domains dominate AI-answer citations for a topic/keyword,
    and optionally check whether a specific domain shows up among them.
    Use this to answer 'who's winning AI search for this topic' or
    'is my competitor cited more than me for X'.

    Read-only: no side effects, safe to retry. Costs 10 quota units/call
    (free tier is 30 units/month shared across every metered tool, so up to 3
    calls to this tool alone if nothing else is used that period).

    Returns: {"keyword", "platform", "top_domains" (list of {"domain",
    "mentions"}, most-cited domains for this keyword/platform, order as
    ranked by the provider), "top_domains_limit" (int, the provider's own
    cap on this list - absence from it is NOT evidence a domain has zero
    citations, only that it did not rank in the top `top_domains_limit`),
    "compare_domain_rank" (int|null, only present when compare_domain was
    passed: the domain's 1-based position in top_domains, or null if it
    did not rank in the top `top_domains_limit`), "country", "language",
    "source_mix" ({"community_pct" (share of these mentions that go to
    community sites such as Reddit, YouTube, X, Quora), "community_domains",
    "other_domains"}: a high community_pct means this topic is won by what
    people say about a brand elsewhere, not by any one site's pages)}.

    This tool's citation universe is the provider's tracked mention corpus
    for the keyword, which is a different measurement from
    analyze_citation_structure's single live answer - the two can
    legitimately disagree on whether a given domain shows up.

    Use check_prompt_coverage instead if you already know which domain you
    care about and just want to know whether it is cited.

    Args:
        keyword: the topic/query to check, e.g. "best project management tool".
        platform: "chat_gpt" or "google" (Google's AI Overview). Defaults
            to chat_gpt. Perplexity and Gemini aren't available - the
            underlying data provider doesn't cover them for this check.
        compare_domain: optional bare domain to look up in the results
            (exact match against the registrable domain, e.g. "notion.so"
            will not match "mynotion.so.example.com").
        country: market to check, e.g. "Italy". Defaults to "United States".
            chat_gpt only has data for the United States; use platform
            "google" for any other country.
        language: language code, e.g. "it". Defaults to "en" (the only
            option for chat_gpt).
    """
    country, language = _market(country, language)
    if err := _guard_platform(platform) or _guard_mentions_market(platform, country, language):
        _log_call("find_citation_leaders", "invalid_platform")
        return {"error": err}
    if err := _guard_balance():
        _log_call("find_citation_leaders", "balance_denied")
        return {"error": err}
    if err := _guard_usage(10):
        _log_call("find_citation_leaders", "quota_denied")
        return {"error": err}
    try:
        result = dfs.citation_leaders(keyword=keyword, platform=platform, country=country, language=language)
    except dfs.DataForSEOError:
        _refund_usage(10)
        _log_call("find_citation_leaders", "provider_error")
        return {
            "error": (
                "Visibility data provider had a transient error on this "
                "request. Try again - if it keeps failing for this keyword/"
                "platform, contact support@vantagemcp.dev."
            )
        }
    if result.get("error"):
        # citation_leaders() returns no "top_domains" key on its own error
        # path, so there is nothing here to accidentally compute
        # compare_domain_rank over. Refund: quota was already spent on a
        # call that came back unusable.
        _refund_usage(10)
        _log_call("find_citation_leaders", "unparseable_response")
        return {
            "keyword": keyword, "platform": platform,
            "error": (
                "Couldn't determine citation leaders for this keyword/platform "
                "(the provider's response wasn't in the expected shape). Try "
                "again, or contact support@vantagemcp.dev if it persists."
            ),
        }
    if compare_domain:
        cd = _normalize_domain(compare_domain)
        rank = next(
            (i for i, d in enumerate(result.get("top_domains", []), start=1)
             if _normalize_domain(d.get("domain") or "") == cd),
            None,
        )
        result["compare_domain_rank"] = rank
    _log_call("find_citation_leaders", "success")
    return result


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def analyze_citation_trend(domain: str, platform: str = "chat_gpt", months: int = 6,
                           country: str = dfs.DEFAULT_COUNTRY, language: str = dfs.DEFAULT_LANGUAGE) -> dict:
    """Track how a domain's AI-citation count has moved month over month,
    so you can see whether visibility is growing or fading instead of
    only ever checking a single point in time. Use this to answer 'is our
    AI visibility improving' or 'did that content push actually move the
    needle'.

    Read-only: no side effects, safe to retry. Costs 1 quota unit/call
    (free tier is 30 units/month shared across every metered tool, so up to 30
    calls to this tool alone if nothing else is used that period).

    Returns: {"domain", "platform", "months" (list of {"year", "month",
    "mentions" (int, 0 for a month with no tracked citations. A zero
    between two large months can be a gap in the provider's history rather
    than a real drop, so read isolated zeros with care), "ai_search_volume"},
    oldest to newest),
    "trend": {"direction" ("up"/"down"/"flat"/"no_data"), "earliest_mentions",
    "latest_mentions", "excluded_current_partial_month" (bool, only present
    and true when the most recent calendar month was excluded from the trend
    calculation because it is still in progress and its count is not yet
    final - it is still returned inside `months`, just not compared)}}.

    The most recent entry in `months` (or `trend.latest_mentions` when the
    current month is not excluded) already IS the current count, so there is
    no need for a separate call just to see it right now.

    Args:
        domain: bare domain to check, e.g. "example.com" (no https://, no www).
        platform: "chat_gpt" or "google" (Google's AI Overview). Defaults
            to chat_gpt. Perplexity and Gemini aren't available - the
            underlying data provider doesn't cover them for this check.
        months: how many recent months of history to return. Defaults to
            6, capped at 13 - DataForSEO's historical data only goes back
            to 2025-08-01.
        country: market to check, e.g. "Italy". Defaults to "United States".
            chat_gpt only has data for the United States; use platform
            "google" for any other country.
        language: language code, e.g. "it". Defaults to "en" (the only
            option for chat_gpt).
    """
    country, language = _market(country, language)
    if err := _guard_platform(platform) or _guard_mentions_market(platform, country, language):
        _log_call("analyze_citation_trend", "invalid_platform")
        return {"error": err}
    if err := _guard_balance():
        _log_call("analyze_citation_trend", "balance_denied")
        return {"error": err}
    if err := _guard_usage(1):
        _log_call("analyze_citation_trend", "quota_denied")
        return {"error": err}
    try:
        result = dfs.citation_trend(domain=domain, platform=platform, country=country, language=language)
    except dfs.DataForSEOError:
        _refund_usage(1)
        _log_call("analyze_citation_trend", "provider_error")
        return {
            "error": (
                "Visibility data provider had a transient error on this "
                "request. Try again - if it keeps failing for this domain/"
                "platform, contact support@vantagemcp.dev."
            )
        }
    if result.get("error"):
        _refund_usage(1)
        _log_call("analyze_citation_trend", "error")
        return result

    window = result.get("months", [])[-max(1, min(months, 13)):]

    # The current calendar month is always partial - checked on day 7 of a
    # month, its mentions count is roughly 7/30 of what it will end up being,
    # not a real reading. Comparing "earliest" to a partial "latest" reported
    # a domain with a strong August as "flat, 0 -> 0" on 2026-09-07 simply
    # because September had barely started. Excluded from the trend
    # calculation, but kept in `months` (labeled) since a caller can still
    # want to see the partial figure it has so far.
    now = datetime.now(timezone.utc)
    current_partial = bool(window) and (window[-1]["year"], window[-1]["month"]) == (now.year, now.month)
    trend_window = window[:-1] if current_partial and len(window) > 1 else window

    if not trend_window:
        trend = {"direction": "no_data"}
    else:
        earliest, latest = trend_window[0]["mentions"], trend_window[-1]["mentions"]
        direction = "flat" if latest == earliest else ("up" if latest > earliest else "down")
        trend = {"direction": direction, "earliest_mentions": earliest, "latest_mentions": latest}
        if current_partial:
            trend["excluded_current_partial_month"] = True

    _log_call("analyze_citation_trend", "success")
    return {"domain": domain, "platform": platform, "country": country, "language": language,
            "months": window, "trend": trend}


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def analyze_citation_structure(keyword: str, country: str = dfs.DEFAULT_COUNTRY,
                               language: str = dfs.DEFAULT_LANGUAGE, engine: str = "chat_gpt",
                               samples: int = 1) -> dict:
    """Analyze the structural shape of the AI-generated answer actually
    cited for a keyword: does it lead with a list, how long is the opening
    passage, how many sources does it cite and from which domains. Use
    this to understand what a winning AI-search answer looks like for a
    topic, e.g. before writing content meant to get cited.

    Read-only: no side effects, safe to retry. Costs 1 quota unit per sample
    (1 by default; free tier is 30 units/month shared across every metered
    tool, so up to 30 single-sample calls to this tool alone if nothing else
    is used that period).

    Returns: {"keyword", "engine", "model" (the answering model's version, as
    the provider reports it), "checked_at" (when the answer was fetched, UTC),
    "leads_with_list" (bool), "opening_word_count"
    (int), "opening_has_number" (bool), "outline" (list of up to 12 section
    heads, in order: the answer's headings, or its top-level list items when it
    has fewer than two headings; heads only, never the text under them),
    "has_table" (bool), "num_sources_cited" (int),
    "source_domains" (list of up to 10 domain strings), "source_mix"
    ({"community_pct" (share of those sources that are community sites such
    as Reddit, YouTube, X, Quora), "community_domains", "other_domains"}),
    "country", "language"}. With samples above 1 the shape fields describe
    the first answer, plus "samples_ok" (answers that came back) and
    "source_frequency" (list of {"domain", "runs"}: how many of the answers
    cited each domain, most often first). Answers change from run to run, so
    a domain cited in every sample is a far stronger signal than one sample.

    Use analyze_citation_structure_batch instead if you need this for more than
    one keyword - one call per topic here adds up fast for a cluster. Use
    analyze_citation_gap instead if you have your own page for this
    keyword and want the gap to the winner, not just the winner's shape.

    Args:
        keyword: the topic/query to analyze, e.g. "how to reduce churn".
        country: market to read the answer in, e.g. "Italy". Defaults to
            "United States". For perplexity a 2-letter code also works.
        language: language code, e.g. "it". Defaults to "en". Write the
            keyword in that language too.
        engine: "chat_gpt" (default), "gemini" or "perplexity". chat_gpt and
            gemini are the answers a person sees in those apps; perplexity is
            Perplexity's sonar API with web search.
        samples: how many independent answers to read, 1 to 5. Default 1.
    """
    country, language = _market(country, language)
    if err := _guard_live(engine, samples):
        _log_call("analyze_citation_structure", "invalid_argument")
        return {"error": err}
    if err := _guard_balance():
        _log_call("analyze_citation_structure", "balance_denied")
        return {"error": err}
    if err := _guard_usage(samples):
        _log_call("analyze_citation_structure", "quota_denied")
        return {"error": err}
    runs = dfs.sample_structures(keyword, samples, country=country, language=language, engine=engine)
    ok = [r for r in runs if not r.get("error")]
    if len(ok) < samples:
        _refund_usage(samples - len(ok))
    if not ok:
        _log_call("analyze_citation_structure", "provider_error" if runs[0].get("transient") else "error")
        return {"keyword": keyword, "error": _run_error(runs[0])}
    result = dict(ok[0])
    if samples > 1:
        result["samples_ok"] = len(ok)
        result["source_frequency"] = _source_frequency(ok)
    _log_call("analyze_citation_structure", "success", engine=engine, samples=samples)
    return result


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def analyze_citation_structure_batch(keywords: list[str], country: str = dfs.DEFAULT_COUNTRY,
                                     language: str = dfs.DEFAULT_LANGUAGE, engine: str = "chat_gpt") -> dict:
    """Analyze the structural shape of the winning AI answer across several
    related keywords/topics in one call: does each lead with a list, how
    long is the opening, how many sources it cites. Use this for content
    planning across a topic cluster, e.g. before writing several related
    pieces meant to get cited, instead of calling analyze_citation_structure once
    per topic.

    Read-only: no side effects, safe to retry. Costs 1 quota unit per
    keyword in the batch (free tier is 30 units/month shared across all
    the metered tools, so up to 30 keywords total that period if nothing else
    is used). A per-keyword provider error doesn't fail the whole batch -
    that keyword's entry just carries an "error" field instead.

    Returns: {"results" (list, one {"keyword", ...same shape as
    analyze_citation_structure, or "error"} per keyword, in the order given),
    "summary": {"topics_analyzed", "topics_requested", "list_led_count",
    "avg_sources_cited", "avg_community_pct" (average source_mix.community_pct
    across the analyzed topics)}}.

    Args:
        keywords: topics/queries to analyze, e.g. ["how to reduce churn",
            "churn rate benchmarks", "reduce customer churn saas"]. Max 10.
        country: market to read the answers in, e.g. "Italy". Defaults to
            "United States".
        language: language code, e.g. "it". Defaults to "en".
        engine: "chat_gpt" (default), "gemini" or "perplexity", as in
            analyze_citation_structure. One answer per topic.
    """
    country, language = _market(country, language)
    if err := _guard_live(engine, 1):
        return {"error": err}
    if not keywords:
        return {
            "error": (
                "keywords list is empty - pass at least one topic/query to "
                "analyze, e.g. [\"how to reduce churn\"]."
            )
        }
    if len(keywords) > 10:
        return {
            "error": (
                f"Max 10 keywords per batch call, got {len(keywords)}. Split "
                "into multiple calls, or use analyze_citation_structure for a single "
                "topic."
            )
        }
    if err := _guard_balance():
        _log_call("analyze_citation_structure_batch", "balance_denied", keyword_count=len(keywords))
        return {"error": err}

    results = []
    for kw in keywords:
        if err := _guard_usage(1):
            _log_call("analyze_citation_structure_batch", "quota_denied", keyword=kw)
            results.append({"keyword": kw, "error": err})
            continue
        try:
            result = dfs.citation_structure(keyword=kw, country=country, language=language, engine=engine)
        except dfs.DataForSEOError:
            # A per-keyword provider error must not sink the whole batch -
            # this call already consumed one unit of usage above, so it is
            # handed back: the keyword got no usable result, and the batch
            # still needs a result entry for it, just one marked failed.
            _refund_usage(1)
            _log_call("analyze_citation_structure_batch", "provider_error", keyword=kw)
            results.append({
                "keyword": kw,
                "error": (
                    "Visibility data provider had a transient error on this "
                    "keyword. The rest of the batch still completed - retry "
                    "just this keyword if you need it."
                ),
            })
            continue
        if result.get("error"):
            _refund_usage(1)
        _log_call("analyze_citation_structure_batch", "error" if result.get("error") else "success", keyword=kw)
        results.append(result)

    analyzed = [r for r in results if "error" not in r]
    summary = {
        "topics_analyzed": len(analyzed),
        "topics_requested": len(keywords),
        "list_led_count": sum(1 for r in analyzed if r.get("leads_with_list")),
        "avg_sources_cited": (
            round(sum(r.get("num_sources_cited", 0) for r in analyzed) / len(analyzed), 1)
            if analyzed else 0
        ),
        "avg_community_pct": (
            round(sum(r["source_mix"]["community_pct"] for r in analyzed) / len(analyzed), 1)
            if analyzed else 0
        ),
    }
    return {"results": results, "summary": summary}


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def check_prompt_coverage(domain: str, keywords: list[str], brand: str | None = None,
                          country: str = dfs.DEFAULT_COUNTRY, language: str = dfs.DEFAULT_LANGUAGE,
                          engine: str = "chat_gpt", samples: int = 1) -> dict:
    """Check which of several prompts/keywords actually cite a specific
    domain, and which ones don't. This is usually the first real question
    in an AI-answer-engine audit - not "what does a winning answer look
    like" (analyze_citation_structure) or "who wins this one topic"
    (find_citation_leaders), but "out of everything we care about, where do
    we already show up, and where are we invisible." Use this first, then
    use analyze_citation_gap on whichever keywords come back not cited to
    see what to actually change.

    Read-only for the caller, safe to retry. Costs 1 quota unit per keyword
    per sample (1 sample by default; free tier is 30 units/month shared
    across every metered tool, so up to 30 keyword checks that period if
    nothing else is used). A per-keyword provider error doesn't fail the
    whole call - that keyword's entry just carries an "error" field instead.
    On the hosted endpoint each keyword's result is remembered for 180 days
    against your API key, so the next check of the same domain and keyword
    reports what changed (see "previous" and "change"); get_check_history
    reads that record back for free.

    Returns: {"domain", "engine", "samples", "keywords_checked" (int,
    excludes any that errored), "keywords_cited" (int), "coverage_pct"
    (float, 0-100), "not_cited" (list of the keyword strings where domain
    was not cited - the actionable list), "newly_cited" and "no_longer_cited"
    (keywords whose status flipped since your last check of them), "results"
    (one entry per keyword, in the order given: {"keyword", "cited" (bool:
    cited in at least half of the samples), "cited_runs" (how many sampled
    answers cited it), "samples_ok" (how many answers came back), "rank"
    (int|null, best 1-based position among the answers' sources, null when
    never cited), "num_sources_cited", "source_domains" (who IS cited, for a
    keyword you are not in), "source_mix" (how much of that is community
    sites such as Reddit, YouTube, X - where to get discussed to close the
    gap), "source_frequency" (only with samples above 1: {"domain", "runs"}
    per domain), "leads_with_list", "opening_word_count", "mentioned" (bool -
    the answer's text names the domain or brand in at least half of the
    samples, whether or not it links to it), "model", "checked_at",
    "previous" ({"checked_at", "cited_runs", "samples", "best_rank"} from
    your last check of this domain and keyword on the same engine and
    market, or null), "change" ("first_check", "up", "down" or "same",
    comparing citation rates)}, or {"keyword", "error"} for one that
    failed), "keywords_mentioned" (int), "mentioned_not_cited" (keywords
    where the answer names you but does not cite you - the model already
    knows you, it just isn't linking you), "mention_terms" (exactly what was
    looked for in the answer text)}. "Cited" and "mentioned" are separate
    claims and are never merged. Answers change from run to run: with
    samples=1 a single answer decides "cited", so use samples=3 before
    telling someone they are or are not cited.

    Args:
        domain: bare domain to check, e.g. "example.com" (no https://, no www).
        keywords: prompts/topics to check it against, e.g.
            ["best project management software", "asana alternatives",
            "free project management tool"]. Max 10.
        brand: optional brand name to look for in the answer text, e.g.
            "Notion". Without it, the domain's first label is used ("notion"
            for notion.so), which can match an ordinary word by accident for
            a dictionary-word domain, so pass the real brand when known.
        country: market to read the answers in, e.g. "Italy". Defaults to
            "United States".
        language: language code, e.g. "it". Defaults to "en". Write the
            keywords in that language too.
        engine: "chat_gpt" (default), "gemini" or "perplexity", as in
            analyze_citation_structure.
        samples: independent answers to read per keyword, 1 to 5. Default 1.
    """
    country, language = _market(country, language)
    if err := _guard_live(engine, samples):
        return {"error": err}
    if not domain or not domain.strip():
        return {"error": "domain is empty - pass a bare domain, e.g. \"example.com\"."}
    if not keywords:
        return {
            "error": (
                "keywords list is empty - pass at least one prompt/topic to "
                "check, e.g. [\"best project management software\"]."
            )
        }
    if len(keywords) > 10:
        return {
            "error": (
                f"Max 10 keywords per call, got {len(keywords)}. Split into "
                "multiple calls."
            )
        }
    if err := _guard_balance():
        _log_call("check_prompt_coverage", "balance_denied", keyword_count=len(keywords))
        return {"error": err}

    target = _normalize_domain(domain)
    # What counts as the answer naming you: the domain itself, plus the brand
    # if given, else the domain's first label as a best guess.
    mention_terms = [target] + ([brand.strip()] if brand and brand.strip() else [target.split(".")[0]])
    results: list[dict | None] = [None] * len(keywords)
    # Quota is charged and refunded here, on the calling thread: the caller's
    # access token lives in a contextvar the worker threads cannot see.
    jobs = []
    for i, kw in enumerate(keywords):
        if err := _guard_usage(samples):
            _log_call("check_prompt_coverage", "quota_denied", keyword=kw)
            results[i] = {"keyword": kw, "error": err}
        else:
            jobs.append(i)

    def fetch(i: int) -> list[dict]:
        return dfs.sample_structures(keywords[i], samples, mention_terms=mention_terms,
                                     country=country, language=language, engine=engine)

    # Keywords in parallel too: ten keywords one after another took minutes.
    # At most 4 x samples requests in flight, inside the provider's limits.
    runs_by_job = {}
    if jobs:
        with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
            runs_by_job = dict(zip(jobs, pool.map(fetch, jobs)))

    client_id = _client_id()
    for i, runs in runs_by_job.items():
        kw = keywords[i]
        ok = [r for r in runs if not r.get("error")]
        if len(ok) < samples:
            _refund_usage(samples - len(ok))
        if not ok:
            _log_call("check_prompt_coverage", "provider_error" if runs[0].get("transient") else "error",
                      keyword=kw)
            results[i] = {"keyword": kw, "error": _run_error(runs[0])}
            continue

        ranks = [next((n for n, d in enumerate(r.get("source_domains") or [], start=1)
                       if _normalize_domain(d) == target), None) for r in ok]
        cited_runs = sum(1 for r in ranks if r is not None)
        best_rank = min((r for r in ranks if r is not None), default=None)
        mentioned_runs = sum(1 for r in ok if r.get("mentioned"))
        first = ok[0]
        entry = {
            "keyword": kw,
            "cited": _cited_majority(cited_runs, len(ok)),
            "cited_runs": cited_runs,
            "samples_ok": len(ok),
            "rank": best_rank,
            "num_sources_cited": first.get("num_sources_cited"),
            "source_domains": first.get("source_domains") or [],
            "source_mix": first.get("source_mix"),
            **({"source_frequency": _source_frequency(ok)} if samples > 1 else {}),
            "leads_with_list": first.get("leads_with_list"),
            "opening_word_count": first.get("opening_word_count"),
            "mentioned": _cited_majority(mentioned_runs, len(ok)),
            "model": first.get("model"),
            "checked_at": first.get("checked_at"),
            "previous": None,
            "change": None,
        }
        if client_id:
            try:
                prev = store.record_check(client_id, target, kw, engine, country, language,
                                          len(ok), cited_runs, best_rank, mentioned_runs)
            except sqlite3.Error:
                prev, entry["change"] = None, "not_recorded"
            else:
                entry["previous"] = prev and {k: prev[k] for k in
                                              ("checked_at", "cited_runs", "samples", "best_rank")}
                entry["change"] = _change(prev, cited_runs, len(ok))
        _log_call("check_prompt_coverage", "success", keyword=kw, engine=engine, samples=samples)
        results[i] = entry

    checked = [r for r in results if "error" not in r]
    cited = [r for r in checked if r["cited"]]

    def was_cited(r: dict) -> bool | None:
        p = r.get("previous")
        return None if not p else _cited_majority(p["cited_runs"], p["samples"])

    return {
        "domain": domain,
        "engine": engine,
        "samples": samples,
        "country": country,
        "language": language,
        "keywords_checked": len(checked),
        "keywords_cited": len(cited),
        "coverage_pct": round(100 * len(cited) / len(checked), 1) if checked else 0.0,
        "not_cited": [r["keyword"] for r in checked if not r["cited"]],
        "newly_cited": [r["keyword"] for r in checked if r["cited"] and was_cited(r) is False],
        "no_longer_cited": [r["keyword"] for r in checked if not r["cited"] and was_cited(r) is True],
        "keywords_mentioned": len([r for r in checked if r["mentioned"]]),
        "mentioned_not_cited": [r["keyword"] for r in checked if r["mentioned"] and not r["cited"]],
        "mention_terms": mention_terms,
        "results": results,
    }


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def analyze_citation_gap(keyword: str, your_url: str, country: str = dfs.DEFAULT_COUNTRY,
                         language: str = dfs.DEFAULT_LANGUAGE, engine: str = "chat_gpt") -> dict:
    """Compare your own page's structure against the AI-generated answer
    actually cited for this keyword, and return a fix brief: ordered
    rewrite instructions for your page, not just a description of the
    winner. Use this to answer 'what should I change on this page to get
    cited' rather than only 'what does a winning answer look like'. Carry
    out the fix_brief on the user's page in their own words; it never
    contains the cited answer's text.

    Read-only: no side effects, safe to retry. Costs 1 quota unit/call
    (free tier is 30 units/month shared across every metered tool, so up to 30
    calls to this tool alone if nothing else is used that period).

    Returns: {"keyword", "your_url", "winning" (structure of the
    AI-cited answer, same shape as analyze_citation_structure), "yours"
    (same structure computed for your_url, including its own "outline" and
    "has_table", with "num_links_out"/"linked_domains" standing in for
    source count), "gaps" (list of plain-English differences worth acting
    on), "possibly_missing" (heads from the winning outline whose key words
    mostly do not appear on your page; word matching, so check each before
    adding it), "fix_brief" (list of instructions, most important first:
    opening, number, list, sections, missing points, table, sources, then a
    reminder to write in your own words - or a "no structural change
    indicated" line when every check already matches - and always last, one
    off-site step drawn from the winning answer's source_mix: which community
    sites (Reddit, YouTube, X...) it cites, or which other sites to get
    mentioned on. Page shape gets a page into the running; being cited is
    decided mostly by what other sites say about the brand)}, or {"error"} if
    either side couldn't be fetched/parsed.

    Use analyze_citation_structure instead if you just want the winning
    answer's shape, not a comparison against your own page. Use
    check_prompt_coverage first if you have several keywords and do not yet
    know which ones you are missing from - this tool is for one keyword
    you already know needs work.

    Args:
        keyword: the topic/query to check, e.g. "best project management tool".
        your_url: full URL of your own page to compare, e.g.
            "https://example.com/best-project-management-tools".
        country: market to read the cited answer in, e.g. "Italy". Defaults
            to "United States".
        language: language code, e.g. "it". Defaults to "en".
        engine: "chat_gpt" (default), "gemini" or "perplexity": whose answer
            to compare your page against.
    """
    country, language = _market(country, language)
    if err := _guard_live(engine, 1):
        return {"error": err}
    if err := _guard_balance():
        _log_call("analyze_citation_gap", "balance_denied")
        return {"error": err}
    if err := _guard_usage(1):
        _log_call("analyze_citation_gap", "quota_denied")
        return {"error": err}
    try:
        result = dfs.citation_gap(keyword=keyword, your_url=your_url, country=country,
                                  language=language, engine=engine)
    except dfs.DataForSEOError:
        _refund_usage(1)
        _log_call("analyze_citation_gap", "provider_error")
        return {
            "error": (
                "Visibility data provider had a transient error on this "
                "request. Try again - if it keeps failing for this keyword/"
                "URL, contact support@vantagemcp.dev."
            )
        }
    if result.get("error"):
        _refund_usage(1)
    _log_call("analyze_citation_gap", "error" if result.get("error") else "success")
    return result


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def find_cited_questions(domain: str, platform: str = "chat_gpt", limit: int = 20,
                         country: str = dfs.DEFAULT_COUNTRY, language: str = dfs.DEFAULT_LANGUAGE) -> dict:
    """Find the questions people ask AI answer engines where a domain is
    already cited as a source, most-asked first. Starts from the domain, so
    nobody has to guess keywords first. Use this to answer 'what does
    ChatGPT already cite us for' or to pick the keywords to feed
    check_prompt_coverage and analyze_citation_gap.

    Read-only: no side effects, safe to retry. Costs 10 quota units/call
    (free tier is 30 units/month shared across every metered tool, so up to 3
    calls to this tool alone if nothing else is used that period).

    Returns: {"domain", "platform", "country", "language", "total_questions"
    (int, every tracked question citing the domain, which can exceed the
    list), "questions" (up to `limit`, most-asked first: {"question",
    "ai_search_volume" (monthly asks as the provider estimates them),
    "your_position" (1-based position of the domain among that answer's
    sources), "source_domains" (who else that answer cites), "last_seen"
    (when the provider last recorded this answer, UTC)})}. An empty list means
    the provider's tracked answers do not cite the domain, not that no
    answer anywhere does.

    This reads the provider's tracked answer corpus, the same measurement as
    find_citation_leaders, not a live answer: re-check a question with
    check_prompt_coverage to see today's answer.

    Args:
        domain: bare domain, e.g. "example.com" (no https://, no www).
            Subdomains are included.
        platform: "chat_gpt" (default) or "google" (Google's AI Overview).
        limit: how many questions to return, 1 to 20. Default 20.
        country: market, e.g. "Italy". Defaults to "United States". chat_gpt
            only has United States data; use platform "google" elsewhere.
        language: language code, e.g. "it". Defaults to "en", the only
            option for chat_gpt.
    """
    country, language = _market(country, language)
    if not domain or not domain.strip():
        return {"error": "domain is empty - pass a bare domain, e.g. \"example.com\"."}
    if err := _guard_platform(platform) or _guard_mentions_market(platform, country, language):
        _log_call("find_cited_questions", "invalid_platform")
        return {"error": err}
    limit = max(1, min(int(limit or 20), 20))
    if err := _guard_balance():
        _log_call("find_cited_questions", "balance_denied")
        return {"error": err}
    if err := _guard_usage(10):
        _log_call("find_cited_questions", "quota_denied")
        return {"error": err}
    try:
        result = dfs.cited_questions(_normalize_domain(domain), platform=platform, limit=limit,
                                     country=country, language=language)
    except dfs.DataForSEOError:
        _refund_usage(10)
        _log_call("find_cited_questions", "provider_error")
        return {"error": TRANSIENT_ERROR}
    if result.get("error"):
        _refund_usage(10)
        _log_call("find_cited_questions", "error")
        return {"domain": domain, "error": TRANSIENT_ERROR}
    _log_call("find_cited_questions", "success")
    return result


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def get_check_history(domain: str, keyword: str | None = None, limit: int = 50) -> dict:
    """Read back your own earlier check_prompt_coverage results for a domain,
    newest first, to show progress over time or to confirm whether a change
    (a rewrite, a new mention somewhere) moved anything. Use this for 'has
    our citation status changed since last week' without spending units.

    Costs 0 quota units: it only reads Vantage's own record of your checks,
    never the data provider. Results are kept 180 days per API key and are
    only visible to that key.

    Returns: {"domain", "keyword" (or null for every keyword), "checks"
    (newest first: {"keyword", "engine", "country", "language", "samples",
    "cited_runs", "best_rank", "mentioned_runs", "checked_at"})}.

    Args:
        domain: the bare domain the checks were run for, e.g. "example.com".
        keyword: optional, only this keyword's history.
        limit: how many rows to return, 1 to 200. Default 50.
    """
    client_id = _client_id()
    if client_id is None:
        return {"error": "Check history is kept per API key, so it only exists on the hosted endpoint."}
    rows = store.check_history(client_id, _normalize_domain(domain), keyword,
                               max(1, min(int(limit or 50), 200)))
    _log_call("get_check_history", "success")
    return {"domain": domain, "keyword": keyword, "checks": rows}


def main() -> None:
    if "--http" in sys.argv:
        # Bound to loopback deliberately - Caddy (or any reverse proxy)
        # owns the public interface and TLS, this process never talks
        # to the open internet directly.
        import os

        port = int(os.environ.get("VANTAGE_PORT", "8420"))
        mcp.run(
            transport="streamable-http",
            host="127.0.0.1",
            port=port,
            transport_security=TRANSPORT_SECURITY,
        )
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
