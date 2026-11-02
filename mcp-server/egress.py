"""Outbound path to the Internet through Zscaler Internet Access (ZIA).

ZIA_EGRESS_MODE
  direct            no proxy: the host's own route (Zscaler Client Connector, GRE/IPsec tunnel, ...)
  proxy             explicit proxy ZIA_PROXY, no credential (ZIA attributes by source IP / Location)
  proxy-token-file  explicit proxy + Proxy-Authorization: Bearer <contents of ZIA_PROXY_TOKEN_FILE>,
                    re-read on every call so an external refresher can rotate it
  proxy-caller      explicit proxy + Proxy-Authorization: Bearer <a token for THE CALLER>, obtained by
                    exchanging the caller's own access token (Entra On-Behalf-Of / Okta token exchange)

The credential rides the proxy CONNECT itself, never the request inside the tunnel. Every failure to
produce a required credential raises EgressDenied: there is deliberately no fallback to another
identity, so nothing leaves unattributed.
"""
import hashlib
import logging
import ssl
import time
from dataclasses import dataclass
from pathlib import Path

import certifi
import httpx

log = logging.getLogger("zia-egress-mcp.egress")

MODES = ("direct", "proxy", "proxy-token-file", "proxy-caller")


class EgressDenied(Exception):
    """No permitted egress credential for this call; the call must not go out."""


def secret(env, name: str) -> str:
    """NAME_FILE (a mounted file) wins over NAME (an env value)."""
    path = env.get(f"{name}_FILE")
    if path:
        return Path(path).read_text().strip()
    return (env.get(name) or "").strip()


def ssl_context(ca_file: str | None) -> ssl.SSLContext:
    """Public roots plus, if given, the Zscaler root CA used by ZIA SSL inspection."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    if ca_file:
        ctx.load_verify_locations(cafile=ca_file)
    return ctx


@dataclass(frozen=True)
class EgressConfig:
    mode: str
    proxy: str = ""              # host:port of the ZIA explicit proxy
    token_file: str = ""         # proxy-token-file
    exchange_kind: str = ""      # proxy-caller: "entra" (On-Behalf-Of) | "okta" (RFC 8693 token exchange)
    token_url: str = ""          # proxy-caller: token endpoint for the exchange
    client_id: str = ""          # proxy-caller: this server's own client (confidential)
    client_secret: str = ""
    scope: str = ""              # proxy-caller: scope of the ZIA egress token
    audience: str = ""           # proxy-caller, Okta only: audience of the ZIA egress token
    timeout: float = 30.0


def egress_from_env(env, idp) -> EgressConfig:
    mode = (env.get("ZIA_EGRESS_MODE") or "").strip().lower()
    if mode not in MODES:
        raise SystemExit(f"ZIA_EGRESS_MODE must be one of {', '.join(MODES)}; got {mode!r}")
    proxy = (env.get("ZIA_PROXY") or "").strip()
    if mode != "direct" and (":" not in proxy or not proxy.rsplit(":", 1)[1].isdigit()):
        raise SystemExit(f"ZIA_EGRESS_MODE={mode} needs ZIA_PROXY=host:port (your ZIA explicit proxy)")
    cfg = EgressConfig(mode=mode, proxy=proxy, timeout=float(env.get("HTTP_TIMEOUT") or 30))
    if mode == "proxy-token-file":
        if not env.get("ZIA_PROXY_TOKEN_FILE"):
            raise SystemExit("ZIA_EGRESS_MODE=proxy-token-file needs ZIA_PROXY_TOKEN_FILE")
        cfg = EgressConfig(**{**cfg.__dict__, "token_file": env["ZIA_PROXY_TOKEN_FILE"]})
    if mode == "proxy-caller":
        if idp is None:
            raise SystemExit("ZIA_EGRESS_MODE=proxy-caller needs a caller token to exchange: MCP_AUTH=entra|okta, "
                             "not apikey")
        need = {"MCP_CLIENT_ID": env.get("MCP_CLIENT_ID"), "MCP_CLIENT_SECRET": secret(env, "MCP_CLIENT_SECRET"),
                "ZIA_EGRESS_SCOPE": env.get("ZIA_EGRESS_SCOPE")}
        if idp.kind == "okta":
            need["ZIA_EGRESS_AUDIENCE"] = env.get("ZIA_EGRESS_AUDIENCE")
        missing = [k for k, v in need.items() if not v]
        if missing:
            raise SystemExit(f"ZIA_EGRESS_MODE=proxy-caller needs {', '.join(missing)}")
        cfg = EgressConfig(**{**cfg.__dict__, "exchange_kind": idp.kind,
                              "token_url": env.get("ZIA_EGRESS_TOKEN_URL") or idp.token_url,
                              "client_id": need["MCP_CLIENT_ID"], "client_secret": need["MCP_CLIENT_SECRET"],
                              "scope": need["ZIA_EGRESS_SCOPE"], "audience": env.get("ZIA_EGRESS_AUDIENCE") or ""})
    return cfg


class Egress:
    def __init__(self, cfg: EgressConfig, verify, idp_http: httpx.AsyncClient):
        self.cfg, self.verify, self.idp_http = cfg, verify, idp_http
        self._cache: dict[str, tuple[str, float]] = {}   # sha256(caller token) -> (egress token, expiry)

    async def client(self, caller=None) -> httpx.AsyncClient:
        """An httpx client whose every connection takes the configured path. Caller closes it."""
        kw = {"timeout": self.cfg.timeout, "verify": self.verify, "trust_env": False}
        if self.cfg.mode == "direct":
            return httpx.AsyncClient(**kw)
        token = await self.proxy_token(caller)
        headers = {"Proxy-Authorization": f"Bearer {token}"} if token else None
        return httpx.AsyncClient(proxy=httpx.Proxy(f"http://{self.cfg.proxy}", headers=headers), **kw)

    async def proxy_token(self, caller) -> str | None:
        if self.cfg.mode == "proxy":
            return None
        if self.cfg.mode == "proxy-token-file":
            try:
                token = Path(self.cfg.token_file).read_text().strip()
            except OSError as e:
                raise EgressDenied(f"ZIA_PROXY_TOKEN_FILE unreadable: {e.strerror}") from None
            if not token:
                raise EgressDenied("ZIA_PROXY_TOKEN_FILE is empty")
            return token
        if caller is None or not getattr(caller, "token", None):
            raise EgressDenied("no caller token to exchange")
        return await self.exchange(caller.token)

    async def exchange(self, subject_token: str) -> str:
        key = hashlib.sha256(subject_token.encode()).hexdigest()
        now = time.time()
        hit = self._cache.get(key)
        if hit and hit[1] - 60 > now:
            return hit[0]
        c = self.cfg
        if c.exchange_kind == "entra":
            form = {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": subject_token,
                    "requested_token_use": "on_behalf_of", "scope": c.scope,
                    "client_id": c.client_id, "client_secret": c.client_secret}
            auth = None
        else:  # okta: RFC 8693 on-behalf-of token exchange, client_secret_basic
            form = {"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                    "subject_token": subject_token,
                    "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                    "scope": c.scope, "audience": c.audience}
            auth = (c.client_id, c.client_secret)
        try:
            r = await self.idp_http.post(c.token_url, data=form, auth=auth)
        except httpx.HTTPError as e:
            raise EgressDenied(f"token exchange unreachable: {type(e).__name__}") from None
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or "access_token" not in body:
            desc = (body.get("error_description") or "").split("\n")[0][:200]
            raise EgressDenied(f"token exchange refused: HTTP {r.status_code} {body.get('error', '')} {desc}".strip())
        token = body["access_token"]
        self._cache = {k: v for k, v in self._cache.items() if v[1] > now}
        self._cache[key] = (token, now + int(body.get("expires_in", 300)))
        return token
