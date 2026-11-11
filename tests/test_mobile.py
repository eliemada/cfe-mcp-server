import asyncio
import base64
import hashlib
import json
import stat
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from cfe_mcp.client import AuthenticationRequired, CfeError
from cfe_mcp.mobile import (
    CLIENT_ID,
    ISSUER,
    JWKS_URL,
    REDIRECT_URI,
    TOKEN_URL,
    Authorization,
    MobileSession,
)
from cfe_mcp.storage import read_private_json, write_private_json


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def identity(signing_key):
    def token(**overrides):
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "test-user",
            "iat": now,
            "exp": now + 300,
        }
        claims.update(overrides)
        return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "test-key"})

    key = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    return token, {"keys": [{**key, "kid": "test-key", "alg": "RS256"}]}


def seed(path, token):
    write_private_json(
        path,
        {
            "kind": "mobile_oauth",
            "id_token": token,
            "subject": "test-user",
            "refresh_token": "private-refresh-old",
            "id_expires_at": 1,
        },
    )


def test_pkce_and_single_use_authorization():
    auth = Authorization()
    query = parse_qs(urlsplit(auth.url()).query)
    expected = base64.urlsafe_b64encode(hashlib.sha256(auth.verifier.encode()).digest())
    assert query["code_challenge"] == [expected.decode().rstrip("=")]
    assert query["code_challenge_method"] == ["S256"]
    assert auth.verifier not in auth.url()
    callback = REDIRECT_URI + "?" + urlencode({"state": auth.state, "code": "one-use"})
    assert auth.code(callback) == "one-use"
    with pytest.raises(AuthenticationRequired):
        auth.code(callback)


@pytest.mark.parametrize(
    "callback",
    [
        "https://evil.example/?state={state}&code=x",
        "fr.cfe.fdsl://oauth.evil.example/?state={state}&code=x",
        "fr.cfe.fdsl://oauth/?state=wrong&code=x",
        "fr.cfe.fdsl://oauth/?state={state}&state=extra&code=x",
        "fr.cfe.fdsl://oauth/?state={state}&code=x&code=y",
        "fr.cfe.fdsl://oauth/?state=%C3%A9&code=x",
    ],
)
def test_callback_validation(callback):
    auth = Authorization()
    with pytest.raises(CfeError):
        auth.code(callback.format(state=auth.state))


async def test_expired_token_refreshes_rotates_and_hides_credentials(tmp_path, identity):
    token, keys = identity
    path = tmp_path / "mobile-session.json"
    seed(path, token(exp=int(time.time()) - 5))
    calls = []

    def handler(request):
        calls.append(request)
        if str(request.url) == JWKS_URL:
            return httpx.Response(200, json=keys)
        assert str(request.url) == TOKEN_URL
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["refresh_token"]
        assert form["refresh_token"] == ["private-refresh-old"]
        assert form["client_id"] == [CLIENT_ID]
        assert "client_secret" not in form
        return httpx.Response(
            200,
            json={
                "id_token": token(),
                "refresh_token": "private-refresh-new",
                "refresh_token_expires_in": 1209600,
                "profile_info": "private-profile",
            },
        )

    async with MobileSession(path, transport=httpx.MockTransport(handler)) as session:
        result = await session.status()
    assert result["authenticated"] and result["refreshed"] and result["refresh_available"]
    assert "private" not in json.dumps(result)
    assert "test-user" not in json.dumps(result)
    saved = read_private_json(path)
    assert saved["refresh_token"] == "private-refresh-new"
    assert "profile_info" not in saved
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(calls) == 2


async def test_fresh_token_does_not_refresh(tmp_path, identity):
    token, keys = identity
    path = tmp_path / "mobile-session.json"
    seed(path, token())

    def handler(request):
        assert str(request.url) == JWKS_URL
        return httpx.Response(200, json=keys)

    async with MobileSession(path, transport=httpx.MockTransport(handler)) as session:
        assert (await session.status())["refreshed"] is False


@pytest.mark.parametrize("failure", ["subject", "timeout", "revoked"])
async def test_refresh_failures_are_safe(tmp_path, identity, failure):
    token, keys = identity
    path = tmp_path / "mobile-session.json"
    seed(path, token())
    before = path.read_bytes()
    posts = []

    def handler(request):
        if str(request.url) == JWKS_URL:
            return httpx.Response(200, json=keys)
        posts.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-upstream-secret", request=request)
        if failure == "revoked":
            return httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": "private-upstream-secret",
                },
            )
        return httpx.Response(200, json={"id_token": token(sub="other-account")})

    async with MobileSession(path, transport=httpx.MockTransport(handler)) as session:
        with pytest.raises(CfeError) as error:
            await session.status(force_refresh=True)
        assert "private" not in str(error.value)
        if failure == "revoked":
            with pytest.raises(AuthenticationRequired):
                await session.status()
            assert read_private_json(path)["reauth_required"]
        else:
            assert path.read_bytes() == before
    assert len(posts) == 1


@pytest.mark.parametrize("invalid", ["nonce", "audience", "signature"])
async def test_invalid_identity_is_not_persisted(tmp_path, identity, invalid):
    token, keys = identity
    auth = Authorization()
    overrides = {"nonce": auth.nonce}
    if invalid == "nonce":
        overrides["nonce"] = "other-nonce"
    if invalid == "audience":
        overrides["aud"] = "other-client"
    signed = token(**overrides)
    if invalid == "signature":
        parts = signed.split(".")
        parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
        signed = ".".join(parts)

    def handler(request):
        if str(request.url) == JWKS_URL:
            return httpx.Response(200, json=keys)
        return httpx.Response(200, json={"id_token": signed, "refresh_token": "private"})

    path = tmp_path / "mobile-session.json"
    async with MobileSession(path, transport=httpx.MockTransport(handler)) as session:
        with pytest.raises(CfeError):
            await session.exchange(
                auth,
                REDIRECT_URI
                + "?"
                + urlencode(
                    {
                        "state": auth.state,
                        "code": "test-code",
                    }
                ),
            )
    assert not path.exists()


async def test_concurrent_clients_share_one_refresh(tmp_path, identity):
    token, keys = identity
    path = tmp_path / "mobile-session.json"
    seed(path, token(exp=int(time.time()) - 5))
    posts = []

    async def handler(request):
        if str(request.url) == JWKS_URL:
            return httpx.Response(200, json=keys)
        posts.append(request)
        await asyncio.sleep(0.02)
        return httpx.Response(200, json={"id_token": token(), "refresh_token": "rotated"})

    async def read():
        async with MobileSession(path, transport=httpx.MockTransport(handler)) as session:
            return await session.status()

    results = await asyncio.gather(read(), read())
    assert len(posts) == 1
    assert sorted(r["refreshed"] for r in results) == [False, True]
