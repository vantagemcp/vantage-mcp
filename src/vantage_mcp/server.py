"""Vantage: check how visible a brand/domain is inside AI answer
engines (ChatGPT, Perplexity, Gemini) directly from an agent session.

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

# All 4 tools share this exact profile: pure reads against a third-party
# data provider, no writes, safe to retry, results depend on external
# (non-deterministic over time) data. One shared constant so the 4
# @mcp.tool() calls below don't repeat identical annotation blocks.
READ_ONLY_EXTERNAL = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

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
        "(ChatGPT, Perplexity, Gemini) and how the winning AI-generated answer "
        "for a given topic is structured. Use this when a user asks things like "
        "'does ChatGPT know about my product', 'who gets cited for this keyword "
        "in AI search', or 'what does a winning AI answer look like for X'."
    ),
    token_verifier=VantageTokenVerifier(),
    auth=AuthSettings(
        issuer_url=BASE_URL,
        resource_server_url=f"{BASE_URL}/mcp",
    ),
)


def _log_call(tool: str, outcome: str, **extra: object) -> None:
    """One structured line per real (metered) tool call, so it's answerable
    later whether a given signup ever had a working call - not just whether
    a key was issued. Captured automatically by systemd/journald, same as
    every other log line this process already emits. Only logs when there's
    a real access token (hosted transport, real customer) - stdio/local-dev
    calls are unmetered and untracked, same scope as the usage guard.
    """
    token = get_access_token()
    if token is None:
        return
    print(json.dumps({
        "ts": time.time(),
        "client_id": token.client_id,
        "tool": tool,
        "outcome": outcome,
        **extra,
    }), flush=True)


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
    given AI platform (chat_gpt, perplexity, gemini). Use this to answer
    'is my brand/domain visible in AI search' or 'does ChatGPT know about us'.

    Read-only: no side effects, safe to retry. Costs 10 quota units/call
    (free tier: 3 checks/month total across all tools).

    Returns: {"domain", "platform", "mentions_found" (int - how many times
    the domain was cited in the provider's tracked answers for this
    platform), "visible" (bool - true if mentions_found > 0)}.

    Use citation_leaders instead if you want a ranked list of who's
    winning for a topic rather than one domain's own count.

    Args:
        domain: bare domain to check, e.g. "example.com" (no https://, no www).
        platform: one of "chat_gpt", "perplexity", "gemini". Defaults to chat_gpt.
    """
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
def citation_leaders(keyword: str, platform: str = "chat_gpt", compare_domain: str | None = None) -> dict:
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
        platform: one of "chat_gpt", "perplexity", "gemini". Defaults to chat_gpt.
        compare_domain: optional bare domain to flag if present in the results.
    """
    if err := _guard_usage(10):
        _log_call("citation_leaders", "quota_denied")
        return {"error": err}
    if err := _guard_balance():
        _log_call("citation_leaders", "balance_denied")
        return {"error": err}
    try:
        result = dfs.citation_leaders(keyword=keyword, platform=platform)
    except dfs.DataForSEOError:
        _log_call("citation_leaders", "provider_error")
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
    _log_call("citation_leaders", "error" if result.get("error") else "success")
    return result


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def citation_structure(keyword: str) -> dict:
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

    Use citation_structure_batch instead if you need this for more than
    one keyword - one call per topic here adds up fast for a cluster.

    Args:
        keyword: the topic/query to analyze, e.g. "how to reduce churn".
    """
    if err := _guard_usage(1):
        _log_call("citation_structure", "quota_denied")
        return {"error": err}
    if err := _guard_balance():
        _log_call("citation_structure", "balance_denied")
        return {"error": err}
    try:
        result = dfs.citation_structure(keyword=keyword)
    except dfs.DataForSEOError:
        _log_call("citation_structure", "provider_error")
        return {
            "error": (
                "Visibility data provider had a transient error on this "
                "request. Try again - if it keeps failing for this keyword, "
                "contact support@vantagemcp.dev."
            )
        }
    _log_call("citation_structure", "error" if result.get("error") else "success")
    return result


@mcp.tool(annotations=READ_ONLY_EXTERNAL)
def citation_structure_batch(keywords: list[str]) -> dict:
    """Analyze the structural shape of the winning AI answer across several
    related keywords/topics in one call: does each lead with a list, how
    long is the opening, how many sources it cites. Use this for content
    planning across a topic cluster, e.g. before writing several related
    pieces meant to get cited, instead of calling citation_structure once
    per topic.

    Read-only: no side effects, safe to retry. Costs 1 quota unit per
    keyword in the batch (free tier: 3 checks/month total across all
    tools). A per-keyword provider error doesn't fail the whole batch -
    that keyword's entry just carries an "error" field instead.

    Returns: {"results" (list, one {"keyword", ...same shape as
    citation_structure, or "error"} per keyword, in the order given),
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
                "into multiple calls, or use citation_structure for a single "
                "topic."
            )
        }
    if err := _guard_balance():
        _log_call("citation_structure_batch", "balance_denied", keyword_count=len(keywords))
        return {"error": err}

    results = []
    for kw in keywords:
        if err := _guard_usage(1):
            _log_call("citation_structure_batch", "quota_denied", keyword=kw)
            results.append({"keyword": kw, "error": err})
            continue
        try:
            result = dfs.citation_structure(keyword=kw)
        except dfs.DataForSEOError:
            # A per-keyword provider error must not sink the whole batch -
            # this call already consumed one unit of usage above, so the
            # keyword still needs a result entry, just one marked failed.
            _log_call("citation_structure_batch", "provider_error", keyword=kw)
            results.append({
                "keyword": kw,
                "error": (
                    "Visibility data provider had a transient error on this "
                    "keyword. The rest of the batch still completed - retry "
                    "just this keyword if you need it."
                ),
            })
            continue
        _log_call("citation_structure_batch", "error" if result.get("error") else "success", keyword=kw)
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
