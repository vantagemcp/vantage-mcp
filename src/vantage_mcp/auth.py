"""Bearer-token verification for the hosted (Streamable HTTP) transport.

Deliberately the SDK's lightweight TokenVerifier protocol, not the full
OAuthAuthorizationServerProvider flow - we're not building a "sign in
with Vantage" authorization server, just checking an API key a customer
was handed after paying. If that ever needs to change (e.g. a real
signup/OAuth flow later), this is the one file to replace.
"""

from mcp.server.auth.provider import AccessToken, TokenVerifier

from vantage_mcp import store


class VantageTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        record = store.verify(token)
        if not record:
            return None
        return AccessToken(
            token=token,
            client_id=record["client_id"],
            scopes=[f"tier:{record['tier']}"],
            subject=record["client_id"],
        )


def tier_from_scopes(scopes: list[str]) -> str:
    for s in scopes:
        if s.startswith("tier:"):
            return s.removeprefix("tier:")
    return "free"
