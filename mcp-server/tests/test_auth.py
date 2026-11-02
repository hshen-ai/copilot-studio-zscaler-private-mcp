import time

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from auth import JwtVerifier, idp_from_env, scopes_of
from tests.conftest import AUDIENCE, ISSUER, OKTA_ISSUER, TENANT


def run(coro):
    import asyncio
    return asyncio.run(coro)


def test_entra_preset_derives_issuer_jwks_and_token_url():
    idp = idp_from_env({"MCP_AUTH": "entra", "ENTRA_TENANT_ID": TENANT, "MCP_AUDIENCE": f"{AUDIENCE}, api://{AUDIENCE}",
                        "MCP_REQUIRED_SCOPES": "mcp.access,other"})
    assert idp.issuer == ISSUER
    assert idp.jwks_uri == f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys"
    assert idp.token_url == f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    assert idp.audiences == (AUDIENCE, f"api://{AUDIENCE}")
    assert idp.required_scopes == ("mcp.access", "other")


def test_okta_preset():
    idp = idp_from_env({"MCP_AUTH": "okta", "OKTA_ISSUER": OKTA_ISSUER + "/", "MCP_AUDIENCE": "api://zia-mcp"})
    assert (idp.issuer, idp.jwks_uri, idp.token_url) == (OKTA_ISSUER, OKTA_ISSUER + "/v1/keys", OKTA_ISSUER + "/v1/token")


@pytest.mark.parametrize("env, msg", [
    ({"MCP_AUTH": "apikey", "MCP_AUDIENCE": "x"}, "entra' or 'okta"),
    ({"MCP_AUTH": "entra", "MCP_AUDIENCE": "x"}, "ENTRA_TENANT_ID"),
    ({"MCP_AUTH": "okta", "MCP_AUDIENCE": "x", "OKTA_ISSUER": "http://insecure"}, "OKTA_ISSUER"),
    ({"MCP_AUTH": "entra", "ENTRA_TENANT_ID": "t"}, "MCP_AUDIENCE"),
])
def test_bad_config_fails_fast(env, msg):
    with pytest.raises(SystemExit, match=msg):
        idp_from_env(env)


def test_scopes_of_entra_okta_and_roles():
    assert scopes_of({"scp": "a b"}) == ["a", "b"]
    assert scopes_of({"scp": ["a", "b"]}) == ["a", "b"]
    assert scopes_of({"roles": ["R"]}) == ["R"]
    assert scopes_of({}) == []


def _verifier(fetch_jwks, **env):
    return JwtVerifier(idp_from_env({"MCP_AUTH": "entra", "ENTRA_TENANT_ID": TENANT, "MCP_AUDIENCE": AUDIENCE, **env}),
                       fetch_jwks)


def test_valid_token_accepted_with_identity(fetch_jwks, make_token):
    tok = make_token()
    c = run(_verifier(fetch_jwks).verify_token(tok))
    assert c.token == tok and c.identity == "alice@example.com" and c.scopes == ["mcp.access"]
    assert c.client_id == "agent-app"


def test_identity_falls_back_through_claims(fetch_jwks, make_token):
    c = run(_verifier(fetch_jwks).verify_token(make_token(email=None, upn="bob@corp")))
    assert c.identity == "bob@corp"
    c = run(_verifier(fetch_jwks).verify_token(make_token(email=None)))
    assert c.identity == "pairwise-sub"


@pytest.mark.parametrize("bad", [
    {"aud": "someone-else"},
    {"iss": "https://login.microsoftonline.com/other/v2.0"},
    {"exp": int(time.time()) - 3600},
    {"exp": None},
])
def test_wrong_claims_rejected(fetch_jwks, make_token, bad):
    assert run(_verifier(fetch_jwks).verify_token(make_token(**bad))) is None


def test_wrong_signature_and_unknown_kid_rejected(fetch_jwks, make_token):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    v = _verifier(fetch_jwks)
    assert run(v.verify_token(make_token(signer=other))) is None
    assert run(v.verify_token(make_token(kid="nope"))) is None
    assert run(v.verify_token("not-a-jwt")) is None


def test_jwks_is_cached(fetch_jwks, make_token):
    v = _verifier(fetch_jwks)
    for _ in range(3):
        assert run(v.verify_token(make_token()))
    assert len(fetch_jwks.calls) == 1
    run(v.verify_token(make_token(kid="nope")))   # unknown kid within the refresh window: no refetch
    assert len(fetch_jwks.calls) == 1
