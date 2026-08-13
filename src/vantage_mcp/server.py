"""Vantage: check how visible a brand/domain is inside AI answer
engines (ChatGPT, Perplexity, Gemini) directly from an agent session.

Run locally over stdio for testing (unauthenticated - trusted local
dev, metering is skipped entirely in this mode):
    python -m vantage_mcp.server

Run as a hosted Streamable HTTP endpoint (what registries need, and
where API-key auth + usage metering actually apply):
    python -m vantage_mcp.server --http
"""

import sys

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

from vantage_mcp import dataforseo_client as dfs
from vantage_mcp import store
from vantage_mcp.auth import VantageTokenVerifier, tier_from_scopes

MIN_BALANCE_USD = 1.0  # same hard-stop guardrail as the source pipeline
BASE_URL = "https://vantagemcp.dev"
DOMAIN = "vantagemcp.dev"

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


def _guard_balance() -> str | None:
    try:
        balance = dfs.read_balance()
    except dfs.DataForSEOError as e:
        return f"Visibility data provider unavailable: {e}"
    if balance < MIN_BALANCE_USD:
        return f"Visibility check temporarily unavailable (provider balance ${balance:.2f} below minimum)."
    return None


def _guard_usage() -> str | None:
    """Metering gate, checked BEFORE any paid DataForSEO call.

    No access token present means stdio/local-dev mode (there's no
    HTTP auth layer to have populated one) - trusted, unmetered. Over
    Streamable HTTP a token is always required, so this always applies
    to real customers.
    """
    token = get_access_token()
    if token is None:
        return None
    tier = tier_from_scopes(token.scopes)
    allowed, _remaining, reason = store.check_and_consume(token.client_id, tier)
    return None if allowed else reason


@mcp.tool()
def check_ai_visibility(domain: str, platform: str = "chat_gpt") -> dict:
    """Check how many times a domain is cited in AI-generated answers on a
    given AI platform (chat_gpt, perplexity, gemini). Use this to answer
    'is my brand/domain visible in AI search' or 'does ChatGPT know about us'.

    Args:
        domain: bare domain to check, e.g. "example.com" (no https://, no www).
        platform: one of "chat_gpt", "perplexity", "gemini". Defaults to chat_gpt.
    """
    if err := _guard_usage():
        return {"error": err}
    if err := _guard_balance():
        return {"error": err}
    mentions = dfs.domain_mentions(domain=domain, platform=platform)
    return {
        "domain": domain,
        "platform": platform,
        "mentions_found": mentions,
        "visible": bool(mentions and mentions > 0),
    }


@mcp.tool()
def citation_leaders(keyword: str, platform: str = "chat_gpt", compare_domain: str | None = None) -> dict:
    """Find which domains dominate AI-answer citations for a topic/keyword,
    and optionally check whether a specific domain shows up among them.
    Use this to answer 'who's winning AI search for this topic' or
    'is my competitor cited more than me for X'.

    Args:
        keyword: the topic/query to check, e.g. "best project management tool".
        platform: one of "chat_gpt", "perplexity", "gemini". Defaults to chat_gpt.
        compare_domain: optional bare domain to flag if present in the results.
    """
    if err := _guard_usage():
        return {"error": err}
    if err := _guard_balance():
        return {"error": err}
    result = dfs.citation_leaders(keyword=keyword, platform=platform)
    if compare_domain:
        result["compare_domain_present"] = any(
            compare_domain in (d.get("domain") or "") for d in result.get("top_domains", [])
        )
    return result


@mcp.tool()
def citation_structure(keyword: str) -> dict:
    """Analyze the structural shape of the AI-generated answer actually
    cited for a keyword: does it lead with a list, how long is the opening
    passage, how many sources does it cite and from which domains. Use
    this to understand what a winning AI-search answer looks like for a
    topic, e.g. before writing content meant to get cited.

    Args:
        keyword: the topic/query to analyze, e.g. "how to reduce churn".
    """
    if err := _guard_usage():
        return {"error": err}
    if err := _guard_balance():
        return {"error": err}
    return dfs.citation_structure(keyword=keyword)


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
