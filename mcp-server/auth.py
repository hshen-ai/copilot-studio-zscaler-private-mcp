"""Inbound authentication: validate the caller's Entra ID or Okta access token (OAuth 2.0 Bearer).

FastMCP calls `JwtVerifier.verify_token` for every request to /mcp. A token is accepted only if its
signature verifies against the IdP's published keys (JWKS) and its `iss`, `aud` and `exp` are right.
Required scopes are enforced by FastMCP itself (AuthSettings.required_scopes -> 403 insufficient_scope).
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import jwt
from mcp.server.auth.provider import AccessToken
from pydantic import Field

log = logging.getLogger("zia-egress-mcp.auth")


@dataclass(frozen=True)
class IdpConfig:
    kind: str                     # "entra" | "okta"
    issuer: str
    jwks_uri: str
    token_url: str                # where this server exchanges a caller token (proxy-caller mode)
    audiences: tuple[str, ...]
    required_scopes: tuple[str, ...] = field(default_factory=tuple)


def idp_from_env(env) -> IdpConfig:
    kind = (env.get("MCP_AUTH") or "").strip().lower()
    audiences = tuple(a.strip() for a in (env.get("MCP_AUDIENCE") or "").split(",") if a.strip())
    scopes = tuple((env.get("MCP_REQUIRED_SCOPES") or "").replace(",", " ").split())
    if not audiences:
        raise SystemExit("MCP_AUDIENCE is required (the application ID / audience your callers' tokens carry)")
    if kind == "entra":
        tid = (env.get("ENTRA_TENANT_ID") or "").strip()
        if not tid:
            raise SystemExit("MCP_AUTH=entra needs ENTRA_TENANT_ID")
        base = f"https://login.microsoftonline.com/{tid}"
        return IdpConfig("entra", f"{base}/v2.0", f"{base}/discovery/v2.0/keys",
                         f"{base}/oauth2/v2.0/token", audiences, scopes)
    if kind == "okta":
        iss = (env.get("OKTA_ISSUER") or "").strip().rstrip("/")
        if not iss.startswith("https://"):
            raise SystemExit("MCP_AUTH=okta needs OKTA_ISSUER, e.g. https://<your-org>.okta.com/oauth2/<as-id>")
        return IdpConfig("okta", iss, f"{iss}/v1/keys", f"{iss}/v1/token", audiences, scopes)
    raise SystemExit(f"MCP_AUTH must be 'entra' or 'okta', got {kind!r}")


class Caller(AccessToken):
    """The validated caller. `token` is the raw bearer token (needed for On-Behalf-Of)."""
    claims: dict = Field(default_factory=dict)

    @property
    def identity(self) -> str:
        c = self.claims
        return c.get("email") or c.get("upn") or c.get("preferred_username") or c.get("sub") or "-"


def scopes_of(claims: dict) -> list[str]:
    scp = claims.get("scp")
    if isinstance(scp, str):          # Entra: space-delimited string
        return scp.split()
    if isinstance(scp, list):         # Okta: JSON array
        return [str(s) for s in scp]
    return list(claims.get("roles") or [])   # Entra app-only tokens carry roles, not scp


class JwtVerifier:
    """Implements FastMCP's TokenVerifier protocol."""

    JWKS_MIN_REFRESH = 300            # an unknown `kid` re-fetches the JWKS at most every 5 minutes

    def __init__(self, idp: IdpConfig, fetch_jwks: Callable[[str], Awaitable[dict]], leeway: int = 60):
        self.idp, self._fetch, self.leeway = idp, fetch_jwks, leeway
        self._keys: dict[str, object] = {}
        self._fetched_at = 0.0

    async def _key(self, kid: str | None):
        if kid not in self._keys and time.time() - self._fetched_at > self.JWKS_MIN_REFRESH:
            jwks = await self._fetch(self.idp.jwks_uri)
            self._keys = {k.get("kid"): jwt.PyJWK(k).key for k in jwks.get("keys", []) if k.get("kty") == "RSA"}
            self._fetched_at = time.time()
        if kid not in self._keys:
            raise jwt.InvalidTokenError(f"unknown signing key kid={kid}")
        return self._keys[kid]

    async def verify_token(self, token: str) -> Caller | None:
        try:
            header = jwt.get_unverified_header(token)
            claims = jwt.decode(
                token, await self._key(header.get("kid")), algorithms=["RS256"],
                audience=list(self.idp.audiences), issuer=self.idp.issuer, leeway=self.leeway,
                options={"require": ["exp", "iss", "aud"]},
            )
        except Exception as e:  # any failure is a rejection; never log the token itself
            log.warning("rejected token: %s: %s", type(e).__name__, e)
            return None
        return Caller(
            token=token,
            client_id=str(claims.get("azp") or claims.get("cid") or claims.get("appid") or "-"),
            scopes=scopes_of(claims),
            expires_at=int(claims["exp"]),
            claims=claims,
        )
