"""Vantage: check how visible a brand/domain is inside AI answer
engines (ChatGPT, Google AI Overview) directly from an agent session.

Run locally over stdio for testing (unauthenticated - trusted local
dev, metering is skipped entirely in this mode):
    python -m vantage_mcp.server

Run as a hosted Streamable HTTP endpoint (what registries need, and
where API-key auth + usage metering actually apply):
    python -m vantage_mcp.server --http
"""

import json
import sys
import time
from datetime import datetime, timezone

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from vantage_mcp import dataforseo_client as dfs
from vantage_mcp import store
from vantage_mcp.auth import VantageTokenVerifier, tier_from_scopes

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

# DataForSEO's llm_mentions endpoint family (what check_ai_visibility and
# find_citation_leaders both call) only ever supports these two - confirmed
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

mcp = MCPServer(
    name="vantage",
    instructions=(
        "Checks whether a brand or domain is cited inside AI answer engines "
        "(ChatGPT, Google AI Overview), how that's changed over time, and how "
        "the winning AI-generated answer for a given topic is structured. Use "
        "this when a user asks things like 'does ChatGPT know about my product', "
        "'who gets cited for this keyword in AI search', 'is our AI visibility "
        "growing', or 'what does a winning AI answer look like for X'."
    ),
    token_verifier=VantageTokenVerifier(),
    auth=AuthSettings(
        issuer_url=BASE_URL,
        resource_server_url=f"{BASE_URL}/mcp",
    ),
)


CALL_LOG_PATH = "/var/log/vantage/calls.jsonl"


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


def _guard_balance() -> str | None:
    try:
        balance = dfs.read_balance()
    except dfs.DataForSEOError:
        return (
            "Visibility data provider temporarily unavailable. Try again in a "
            "few minutes - if it persists, contact support@vantagemcp.dev."
        )
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


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def get_usage() -> dict:
    """Check how much of this billing period's quota is left, before
    spending any of it. Use this to answer 'how many checks do I have left'
    or to decide whether a batch call will fit before running it.

    Costs 0 quota units - this never touches the paid data provider, it
    only reads Vantage's own record of what has been used.

    Returns: {"tier", "period" (YYYY-MM), "units_used", "units_limit",
    "units_remaining"}. check_ai_visibility and find_citation_leaders cost
    10 units/call; analyze_citation_trend, analyze_citation_structure (and
    its batch form, per keyword), and analyze_citation_gap cost 1.

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
def check_ai_visibility(domain: str, platform: str = "chat_gpt") -> dict:
    """DEPRECATED - still works, but prefer another tool below. Kept callable
    for anyone already relying on it; not recommended for a new integration.

    Checks how many times a domain is cited in AI-generated answers on a
    given AI platform (chat_gpt, google), as one bare count with no context.

    Why deprecated: it costs 10 units, the same as find_citation_leaders, for
    a single number with no time context and no comparison. If you want to
    know whether a domain shows up in the answers that matter for it, use
    check_prompt_coverage (1 unit per keyword) - it gives cited/not-cited per
    keyword, ranked, across as many prompts as you actually care about,
    for less than the cost of one call here. If you want the count over
    time, analyze_citation_trend already returns this same number monthly,
    including right now, for 1 unit.

    Read-only: no side effects, safe to retry. Costs 10 quota units/call
    (free tier is 30 units/month shared across every metered tool, so up to 3
    calls to this tool alone if nothing else is used that period).

    Returns: {"domain", "platform", "mentions_found" (int - how many times
    the domain was cited in the provider's tracked answers for this
    platform), "visible" (bool - true if mentions_found > 0)}.

    Use check_prompt_coverage instead for "is my brand cited" across the
    prompts you actually care about, at a tenth of the cost per check. Use
    analyze_citation_trend instead for this same count with history attached.
    Use find_citation_leaders instead if you want a ranked list of who's
    winning a topic rather than one domain's own count.

    Args:
        domain: bare domain to check, e.g. "example.com" (no https://, no www).
        platform: "chat_gpt" or "google" (Google's AI Overview). Defaults
            to chat_gpt. Perplexity and Gemini aren't available - the
            underlying data provider doesn't cover them for this check.
    """
    if err := _guard_platform(platform):
        _log_call("check_ai_visibility", "invalid_platform")
        return {"error": err}
    if err := _guard_balance():
        _log_call("check_ai_visibility", "balance_denied")
        return {"error": err}
    if err := _guard_usage(10):
        _log_call("check_ai_visibility", "quota_denied")
        return {"error": err}
    try:
        mentions = dfs.domain_mentions(domain=domain, platform=platform)
    except dfs.DataForSEOError:
        _refund_usage(10)
        _log_call("check_ai_visibility", "provider_error")
        return {
            "error": (
                "Visibility data provider had a transient error on this "
                "request. Try again - if it keeps failing for this domain/"
                "platform, contact support@vantagemcp.dev."
            )
        }
    if mentions is None:
        # domain_mentions() returns None when the provider's response
        # shape was unexpected, not when it confirmed zero mentions -
        # those are different answers and shouldn't look the same.
        _refund_usage(10)
        _log_call("check_ai_visibility", "unparseable_response")
        return {
            "error": (
                "Couldn't determine visibility for this domain/platform "
                "(the provider's response wasn't in the expected shape). "
                "Not the same as confirmed-zero-mentions - try again, or "
                "contact support@vantagemcp.dev if it persists."
            )
        }
    _log_call("check_ai_visibility", "success")
    return {
        "domain": domain,
        "platform": platform,
        "mentions_found": mentions,
        "visible": mentions > 0,
    }


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def find_citation_leaders(keyword: str, platform: str = "chat_gpt", compare_domain: str | None = None) -> dict:
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
    did not rank in the top `top_domains_limit`)}.

    This tool's citation universe is the provider's tracked mention corpus
    for the keyword, which is a different measurement from
    analyze_citation_structure's single live answer - the two can
    legitimately disagree on whether a given domain shows up.

    Use check_prompt_coverage instead if you already know which domain you
    care about and just want to know whether it is cited (check_ai_visibility
    also answers this, but is deprecated - see its own docstring).

    Args:
        keyword: the topic/query to check, e.g. "best project management tool".
        platform: "chat_gpt" or "google" (Google's AI Overview). Defaults
            to chat_gpt. Perplexity and Gemini aren't available - the
            underlying data provider doesn't cover them for this check.
        compare_domain: optional bare domain to look up in the results
            (exact match against the registrable domain, e.g. "notion.so"
            will not match "mynotion.so.example.com").
    """
    if err := _guard_platform(platform):
        _log_call("find_citation_leaders", "invalid_platform")
        return {"error": err}
    if err := _guard_balance():
        _log_call("find_citation_leaders", "balance_denied")
        return {"error": err}
    if err := _guard_usage(10):
        _log_call("find_citation_leaders", "quota_denied")
        return {"error": err}
    try:
        result = dfs.citation_leaders(keyword=keyword, platform=platform)
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
def analyze_citation_trend(domain: str, platform: str = "chat_gpt", months: int = 6) -> dict:
    """Track how a domain's AI-citation count has moved month over month,
    so you can see whether visibility is growing or fading instead of
    only ever checking a single point in time. Use this to answer 'is our
    AI visibility improving' or 'did that content push actually move the
    needle'.

    Read-only: no side effects, safe to retry. Costs 1 quota unit/call
    (free tier is 30 units/month shared across every metered tool, so up to 30
    calls to this tool alone if nothing else is used that period).

    Returns: {"domain", "platform", "months" (list of {"year", "month",
    "mentions" (int, 0 for a month with no tracked citations - a real
    measured zero, not a gap), "ai_search_volume"}, oldest to newest),
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
    """
    if err := _guard_platform(platform):
        _log_call("analyze_citation_trend", "invalid_platform")
        return {"error": err}
    if err := _guard_balance():
        _log_call("analyze_citation_trend", "balance_denied")
        return {"error": err}
    if err := _guard_usage(1):
        _log_call("analyze_citation_trend", "quota_denied")
        return {"error": err}
    try:
        result = dfs.citation_trend(domain=domain, platform=platform)
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
    return {"domain": domain, "platform": platform, "months": window, "trend": trend}


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def analyze_citation_structure(keyword: str) -> dict:
    """Analyze the structural shape of the AI-generated answer actually
    cited for a keyword: does it lead with a list, how long is the opening
    passage, how many sources does it cite and from which domains. Use
    this to understand what a winning AI-search answer looks like for a
    topic, e.g. before writing content meant to get cited.

    Read-only: no side effects, safe to retry. Costs 1 quota unit/call
    (free tier is 30 units/month shared across every metered tool, so up to 30
    calls to this tool alone if nothing else is used that period).

    Returns: {"keyword", "leads_with_list" (bool), "opening_word_count"
    (int), "opening_has_number" (bool), "num_sources_cited" (int),
    "source_domains" (list of up to 10 domain strings)}.

    Use analyze_citation_structure_batch instead if you need this for more than
    one keyword - one call per topic here adds up fast for a cluster. Use
    analyze_citation_gap instead if you have your own page for this
    keyword and want the gap to the winner, not just the winner's shape.

    Args:
        keyword: the topic/query to analyze, e.g. "how to reduce churn".
    """
    if err := _guard_balance():
        _log_call("analyze_citation_structure", "balance_denied")
        return {"error": err}
    if err := _guard_usage(1):
        _log_call("analyze_citation_structure", "quota_denied")
        return {"error": err}
    try:
        result = dfs.citation_structure(keyword=keyword)
    except dfs.DataForSEOError:
        _refund_usage(1)
        _log_call("analyze_citation_structure", "provider_error")
        return {
            "error": (
                "Visibility data provider had a transient error on this "
                "request. Try again - if it keeps failing for this keyword, "
                "contact support@vantagemcp.dev."
            )
        }
    if result.get("error"):
        _refund_usage(1)
    _log_call("analyze_citation_structure", "error" if result.get("error") else "success")
    return result


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def analyze_citation_structure_batch(keywords: list[str]) -> dict:
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
    "avg_sources_cited"}}.

    Args:
        keywords: topics/queries to analyze, e.g. ["how to reduce churn",
            "churn rate benchmarks", "reduce customer churn saas"]. Max 10.
    """
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
            result = dfs.citation_structure(keyword=kw)
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
    }
    return {"results": results, "summary": summary}


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def check_prompt_coverage(domain: str, keywords: list[str]) -> dict:
    """Check which of several prompts/keywords actually cite a specific
    domain, and which ones don't. This is usually the first real question
    in an AI-answer-engine audit - not "what does a winning answer look
    like" (analyze_citation_structure) or "who wins this one topic"
    (find_citation_leaders), but "out of everything we care about, where do
    we already show up, and where are we invisible." Use this first, then
    use analyze_citation_gap on whichever keywords come back not cited to
    see what to actually change.

    Read-only: no side effects, safe to retry. Costs 1 quota unit per
    keyword checked (free tier is 30 units/month shared across every
    metered tool, so up to 30 keywords total that period if nothing else
    is used). A per-keyword provider error doesn't fail the whole call -
    that keyword's entry just carries an "error" field instead. ChatGPT
    only - the underlying check has no Google AI Overview equivalent.

    Returns: {"domain", "keywords_checked" (int, excludes any that
    errored), "keywords_cited" (int), "coverage_pct" (float, 0-100),
    "not_cited" (list of the keyword strings where domain did not appear -
    the actionable list), "results" (one entry per keyword, in the order
    given: {"keyword", "cited" (bool), "rank" (int|null, 1-based position
    among that answer's sources - present even when cited is false, so you
    can tell "just missed it" from "not in the running"), "num_sources_cited",
    "source_domains" (who IS cited, for a keyword you are not in), "leads_with_list",
    "opening_word_count"}, or {"keyword", "error"} for one that failed)}.

    Args:
        domain: bare domain to check, e.g. "example.com" (no https://, no www).
        keywords: prompts/topics to check it against, e.g.
            ["best project management software", "asana alternatives",
            "free project management tool"]. Max 10.
    """
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
    results = []
    for kw in keywords:
        if err := _guard_usage(1):
            _log_call("check_prompt_coverage", "quota_denied", keyword=kw)
            results.append({"keyword": kw, "error": err})
            continue
        try:
            result = dfs.citation_structure(keyword=kw)
        except dfs.DataForSEOError:
            # Same shape as analyze_citation_structure_batch: a per-keyword
            # provider error must not sink the whole call, and the unit
            # already spent above on a call that came back unusable is
            # handed back.
            _refund_usage(1)
            _log_call("check_prompt_coverage", "provider_error", keyword=kw)
            results.append({
                "keyword": kw,
                "error": (
                    "Visibility data provider had a transient error on this "
                    "keyword. The rest of the call still completed - retry "
                    "just this keyword if you need it."
                ),
            })
            continue
        if result.get("error"):
            _refund_usage(1)
            _log_call("check_prompt_coverage", "error", keyword=kw)
            results.append({"keyword": kw, "error": result["error"]})
            continue

        domains = result.get("source_domains") or []
        rank = next(
            (i for i, d in enumerate(domains, start=1) if _normalize_domain(d) == target),
            None,
        )
        _log_call("check_prompt_coverage", "success", keyword=kw)
        results.append({
            "keyword": kw,
            "cited": rank is not None,
            "rank": rank,
            "num_sources_cited": result.get("num_sources_cited"),
            "source_domains": domains,
            "leads_with_list": result.get("leads_with_list"),
            "opening_word_count": result.get("opening_word_count"),
        })

    checked = [r for r in results if "error" not in r]
    cited = [r for r in checked if r["cited"]]
    return {
        "domain": domain,
        "keywords_checked": len(checked),
        "keywords_cited": len(cited),
        "coverage_pct": round(100 * len(cited) / len(checked), 1) if checked else 0.0,
        "not_cited": [r["keyword"] for r in checked if not r["cited"]],
        "results": results,
    }


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def analyze_citation_gap(keyword: str, your_url: str) -> dict:
    """Compare your own page's structure against the AI-generated answer
    actually cited for this keyword, and return concrete gaps to close
    instead of just describing the winner. Use this to answer 'what
    should I change on this page to get cited' rather than only 'what
    does a winning answer look like'.

    Read-only: no side effects, safe to retry. Costs 1 quota unit/call
    (free tier is 30 units/month shared across every metered tool, so up to 30
    calls to this tool alone if nothing else is used that period).

    Returns: {"keyword", "your_url", "winning" (structure of the
    AI-cited answer, same shape as analyze_citation_structure), "yours"
    (same structure computed for your_url, "num_links_out"/
    "linked_domains" standing in for source count), "gaps" (list of
    plain-English differences worth acting on)}, or {"error"} if either
    side couldn't be fetched/parsed.

    Use analyze_citation_structure instead if you just want the winning
    answer's shape, not a comparison against your own page. Use
    check_prompt_coverage first if you have several keywords and do not yet
    know which ones you are missing from - this tool is for one keyword
    you already know needs work.

    Args:
        keyword: the topic/query to check, e.g. "best project management tool".
        your_url: full URL of your own page to compare, e.g.
            "https://example.com/best-project-management-tools".
    """
    if err := _guard_balance():
        _log_call("analyze_citation_gap", "balance_denied")
        return {"error": err}
    if err := _guard_usage(1):
        _log_call("analyze_citation_gap", "quota_denied")
        return {"error": err}
    try:
        result = dfs.citation_gap(keyword=keyword, your_url=your_url)
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
