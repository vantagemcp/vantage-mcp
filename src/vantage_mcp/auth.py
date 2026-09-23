"""Authentication for the hosted (Streamable HTTP) transport.

Two ways in, one kind of token. A customer can paste the API key they were
handed (the only way before 1.8.0), or an MCP client can run the standard
OAuth sign-in: it registers itself, sends the person to Vantage's consent
page, and receives an access token. That access token IS an ordinary Vantage
API key (store.add_key_for_client), so every check, metering rule and tier
applies to it unchanged, and load_access_token verifies both kinds the same
way.

The consent page (server.py, /oauth/consent) accepts either an existing API
key, which signs in to that account, or an email for a new free account.
An email that already has an account is refused rather than signed in:
knowing an address is not proof of owning it, and signing in by email alone
would hand a paying customer's account to anyone who typed their address.
This is the same rule the free signup page has always applied.

The SDK serves discovery, registration, /authorize and /token (with PKCE);
this module only stores things and issues keys. There are no refresh tokens
and no expiry: the token is a long-lived API key, like every other one.
"""

import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from vantage_mcp import store

CODE_TTL_S = 300
PENDING_TTL_S = 900
SCOPE = "vantage"


def _access_token(token: str) -> AccessToken | None:
    record = store.verify(token)
    if not record:
        return None
    return AccessToken(
        token=token,
        client_id=record["client_id"],
        scopes=[f"tier:{record['tier']}"],
        subject=record["client_id"],
    )


class VantageOAuthProvider:
    """The SDK's OAuthAuthorizationServerProvider protocol, backed by store.py."""

    def __init__(self, base_url: str):
        self.base_url = base_url

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = store.oauth_get_client(client_id)
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        store.oauth_put("oauth_clients", client_info.client_id, client_info.model_dump_json())

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request_id = secrets.token_urlsafe(24)
        store.oauth_put_pending(request_id, client.client_id, params.model_dump_json(),
                                time.time() + PENDING_TTL_S)
        return f"{self.base_url}/oauth/consent?req={request_id}"

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        raw = store.oauth_peek_code(authorization_code)
        if not raw:
            return None
        code = AuthorizationCode.model_validate_json(raw)
        return code if code.client_id == client.client_id else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        raw = store.oauth_take_code(authorization_code.code)
        if not raw:
            raise TokenError("invalid_grant", "authorization code already used or expired")
        plaintext = store.add_key_for_client(authorization_code.subject or "")
        if not plaintext:
            raise TokenError("invalid_grant", "the Vantage account for this code no longer exists")
        return OAuthToken(access_token=plaintext, token_type="Bearer", scope=SCOPE)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        return None

    async def exchange_refresh_token(self, client, refresh_token, scopes) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "Vantage tokens do not expire, so there is nothing to refresh")

    async def load_access_token(self, token: str) -> AccessToken | None:
        return _access_token(token)

    async def revoke_token(self, token) -> None:
        return None


def complete_consent(request_id: str, vantage_client_id: str) -> str | None:
    """Finish a pending sign-in for an account and return the redirect URL
    back to the MCP client (carrying a one-time code), or None if the request
    expired or was already used."""
    pending = store.oauth_take_pending(request_id)
    if not pending:
        return None
    oauth_client_id, raw = pending
    params = AuthorizationParams.model_validate_json(raw)
    code = secrets.token_urlsafe(32)
    expires_at = time.time() + CODE_TTL_S
    store.oauth_put("oauth_codes", code, AuthorizationCode(
        code=code,
        scopes=params.scopes or [SCOPE],
        expires_at=expires_at,
        client_id=oauth_client_id,
        code_challenge=params.code_challenge,
        redirect_uri=params.redirect_uri,
        redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
        resource=params.resource,
        subject=vantage_client_id,
    ).model_dump_json(), expires_at)
    return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)


def deny_consent(request_id: str) -> str | None:
    """The person cancelled: send the MCP client the standard access_denied."""
    pending = store.oauth_take_pending(request_id)
    if not pending:
        return None
    params = AuthorizationParams.model_validate_json(pending[1])
    return construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state)


def tier_from_scopes(scopes: list[str]) -> str:
    for s in scopes:
        if s.startswith("tier:"):
            return s.removeprefix("tier:")
    return "free"
