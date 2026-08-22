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
    plain file so an external tool-health check can read recent outcomes
    without journal-read permissions. Only logs when there's a real access
    token (hosted transport, real customer) - stdio/local-dev calls are
    unmetered and untracked, same scope as the usage guard.
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
    """Metering gate, checked BEFORE any paid DataForSEO call.

    No access token present means stdio/local-dev mode (there's no
    HTTP auth layer to have populated one) - trusted, unmetered. Over
    Streamable HTTP a token is always required, so this always applies
    to real customers. `cost` is the calling tool's unit weight (10 for
    the two expensive DataForSEO calls, 1 for the two cheap structure
    ones) - see TIER_LIMITS in store.py for why.
    """
    token = get_access_token()
    if token is None:
        return None
    tier = tier_from_scopes(token.scopes)
    allowed, _remaining, reason = store.check_and_consume(token.client_id, tier, cost)
    return None if allowed else reason


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def check_ai_visibility(domain: str, platform: str = "chat_gpt") -> dict:
    """Check how many times a domain is cited in AI-generated answers on a
    given AI platform (chat_gpt, google). Use this to answer
    'is my brand/domain visible in AI search' or 'does ChatGPT know about us'.

    Read-only: no side effects, safe to retry. Costs 10 quota units/call
    (free tier: 3 checks/month total across all tools).

    Returns: {"domain", "platform", "mentions_found" (int - how many times
    the domain was cited in the provider's tracked answers for this
    platform), "visible" (bool - true if mentions_found > 0)}.

    Use find_citation_leaders instead if you want a ranked list of who's
    winning for a topic rather than one domain's own count. Use
    analyze_citation_trend instead if you want to see this count change
    over time rather than right now.

    Args:
        domain: bare domain to check, e.g. "example.com" (no https://, no www).
        platform: "chat_gpt" or "google" (Google's AI Overview). Defaults
            to chat_gpt. Perplexity and Gemini aren't available - the
            underlying data provider doesn't cover them for this check.
    """
    if err := _guard_platform(platform):
        _log_call("check_ai_visibility", "invalid_platform")
        return {"error": err}
    if err := _guard_usage(10):
        _log_call("check_ai_visibility", "quota_denied")
        return {"error": err}
    if err := _guard_balance():
        _log_call("check_ai_visibility", "balance_denied")
        return {"error": err}
    try:
        mentions = dfs.domain_mentions(domain=domain, platform=platform)
    except dfs.DataForSEOError:
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
    (free tier: 3 checks/month total across all tools).

    Returns: {"keyword", "platform", "top_domains" (list of {"domain",
    "mentions"}, most-cited domains for this keyword/platform, order as
    ranked by the provider), "compare_domain_present" (bool, only present
    when compare_domain was passed)}.

    Use check_ai_visibility instead if you already know which domain you
    care about and just want its own citation count, not a leaderboard.

    Args:
        keyword: the topic/query to check, e.g. "best project management tool".
        platform: "chat_gpt" or "google" (Google's AI Overview). Defaults
            to chat_gpt. Perplexity and Gemini aren't available - the
            underlying data provider doesn't cover them for this check.
        compare_domain: optional bare domain to flag if present in the results.
    """
    if err := _guard_platform(platform):
        _log_call("find_citation_leaders", "invalid_platform")
        return {"error": err}
    if err := _guard_usage(10):
        _log_call("find_citation_leaders", "quota_denied")
        return {"error": err}
    if err := _guard_balance():
        _log_call("find_citation_leaders", "balance_denied")
        return {"error": err}
    try:
        result = dfs.citation_leaders(keyword=keyword, platform=platform)
    except dfs.DataForSEOError:
        _log_call("find_citation_leaders", "provider_error")
        return {
            "error": (
                "Visibility data provider had a transient error on this "
                "request. Try again - if it keeps failing for this keyword/"
                "platform, contact support@vantagemcp.dev."
            )
        }
    if compare_domain:
        result["compare_domain_present"] = any(
            compare_domain in (d.get("domain") or "") for d in result.get("top_domains", [])
        )
    _log_call("find_citation_leaders", "error" if result.get("error") else "success")
    return result


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def analyze_citation_trend(domain: str, platform: str = "chat_gpt", months: int = 6) -> dict:
    """Track how a domain's AI-citation count has moved month over month,
    so you can see whether visibility is growing or fading instead of
    only ever checking a single point in time. Use this to answer 'is our
    AI visibility improving' or 'did that content push actually move the
    needle'.

    Read-only: no side effects, safe to retry. Costs 1 quota unit/call
    (free tier: 3 checks/month total across all tools).

    Returns: {"domain", "platform", "months" (list of {"year", "month",
    "mentions" (int, 0 for a month with no tracked citations - a real
    measured zero, not a gap), "ai_search_volume"}, oldest to newest),
    "trend": {"direction" ("up"/"down"/"flat"/"no_data"),
    "earliest_mentions", "latest_mentions"}}.

    Use check_ai_visibility instead if you only need the current count,
    not how it's changed over time.

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
    if err := _guard_usage(1):
        _log_call("analyze_citation_trend", "quota_denied")
        return {"error": err}
    if err := _guard_balance():
        _log_call("analyze_citation_trend", "balance_denied")
        return {"error": err}
    try:
        result = dfs.citation_trend(domain=domain, platform=platform)
    except dfs.DataForSEOError:
        _log_call("analyze_citation_trend", "provider_error")
        return {
            "error": (
                "Visibility data provider had a transient error on this "
                "request. Try again - if it keeps failing for this domain/"
                "platform, contact support@vantagemcp.dev."
            )
        }
    if result.get("error"):
        _log_call("analyze_citation_trend", "error")
        return result

    window = result.get("months", [])[-max(1, min(months, 13)):]
    if not window:
        trend = {"direction": "no_data"}
    else:
        earliest, latest = window[0]["mentions"], window[-1]["mentions"]
        direction = "flat" if latest == earliest else ("up" if latest > earliest else "down")
        trend = {"direction": direction, "earliest_mentions": earliest, "latest_mentions": latest}

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
    (free tier: 3 checks/month total across all tools).

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
    if err := _guard_usage(1):
        _log_call("analyze_citation_structure", "quota_denied")
        return {"error": err}
    if err := _guard_balance():
        _log_call("analyze_citation_structure", "balance_denied")
        return {"error": err}
    try:
        result = dfs.citation_structure(keyword=keyword)
    except dfs.DataForSEOError:
        _log_call("analyze_citation_structure", "provider_error")
        return {
            "error": (
                "Visibility data provider had a transient error on this "
                "request. Try again - if it keeps failing for this keyword, "
                "contact support@vantagemcp.dev."
            )
        }
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
    keyword in the batch (free tier: 3 checks/month total across all
    tools). A per-keyword provider error doesn't fail the whole batch -
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
            # this call already consumed one unit of usage above, so the
            # keyword still needs a result entry, just one marked failed.
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
def analyze_citation_gap(keyword: str, your_url: str) -> dict:
    """Compare your own page's structure against the AI-generated answer
    actually cited for this keyword, and return concrete gaps to close
    instead of just describing the winner. Use this to answer 'what
    should I change on this page to get cited' rather than only 'what
    does a winning answer look like'.

    Read-only: no side effects, safe to retry. Costs 1 quota unit/call
    (free tier: 3 checks/month total across all tools).

    Returns: {"keyword", "your_url", "winning" (structure of the
    AI-cited answer, same shape as analyze_citation_structure), "yours"
    (same structure computed for your_url, "num_links_out"/
    "linked_domains" standing in for source count), "gaps" (list of
    plain-English differences worth acting on)}, or {"error"} if either
    side couldn't be fetched/parsed.

    Use analyze_citation_structure instead if you just want the winning
    answer's shape, not a comparison against your own page.

    Args:
        keyword: the topic/query to check, e.g. "best project management tool".
        your_url: full URL of your own page to compare, e.g.
            "https://example.com/best-project-management-tools".
    """
    if err := _guard_usage(1):
        _log_call("analyze_citation_gap", "quota_denied")
        return {"error": err}
    if err := _guard_balance():
        _log_call("analyze_citation_gap", "balance_denied")
        return {"error": err}
    try:
        result = dfs.citation_gap(keyword=keyword, your_url=your_url)
    except dfs.DataForSEOError:
        _log_call("analyze_citation_gap", "provider_error")
        return {
            "error": (
                "Visibility data provider had a transient error on this "
                "request. Try again - if it keeps failing for this keyword/"
                "URL, contact support@vantagemcp.dev."
            )
        }
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
