"""Verified native OAuth flow. No Dataverse service credentials or claims API calls."""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt

from .client import AuthenticationRequired, CfeError
from .storage import data_dir, read_private_json, write_private_json

HOST = "cfeb2cprod.b2clogin.com"
CLIENT_ID = "793871d1-9278-40a6-aa15-096bfa55c238"
POLICY_BASE = f"https://{HOST}/cfeb2cprod.onmicrosoft.com/b2c_1a_login_username"
AUTHORIZE_URL = f"{POLICY_BASE}/oauth2/v2.0/authorize"
TOKEN_URL = f"{POLICY_BASE}/oauth2/v2.0/token"
JWKS_URL = f"{POLICY_BASE}/discovery/v2.0/keys"
ISSUER = f"https://{HOST}/tfp/6608f299-39dd-4a03-a510-a013ac24f96b/b2c_1a_login_username/v2.0/"
REDIRECT_URI = "fr.cfe.fdsl://oauth/"
SCOPE = "openid offline_access"
LOGIN_REQUIRED = "Reconnecter le compte avec cfe_mobile_start_login ou `cfe mobile-login`."


@dataclass(repr=False)
class Authorization:
    state: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(48))
    deadline: float = field(default_factory=lambda: time.monotonic() + 900)
    consumed: bool = False

    def url(self) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest())
        return (
            AUTHORIZE_URL
            + "?"
            + urlencode(
                {
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT_URI,
                    "response_type": "code",
                    "response_mode": "query",
                    "scope": SCOPE,
                    "state": self.state,
                    "nonce": self.nonce,
                    "code_challenge": challenge.decode().rstrip("="),
                    "code_challenge_method": "S256",
                    "prompt": "login",
                }
            )
        )

    def code(self, callback: str) -> str:
        if self.consumed or time.monotonic() > self.deadline:
            raise AuthenticationRequired("Autorisation expirée ou déjà utilisée. " + LOGIN_REQUIRED)
        parsed = urlsplit(callback)
        if (parsed.scheme, parsed.netloc, parsed.path, parsed.fragment) != (
            "fr.cfe.fdsl",
            "oauth",
            "/",
            "",
        ):
            raise CfeError("Adresse de retour de connexion mobile invalide.")
        params = parse_qs(parsed.query)
        states = params.get("state", [])
        if (
            len(states) != 1
            or not states[0].isascii()
            or not secrets.compare_digest(states[0], self.state)
        ):
            raise CfeError("État de connexion mobile invalide.")
        self.consumed = True
        if "error" in params:
            raise AuthenticationRequired("Autorisation mobile refusée ou annulée.")
        codes = params.get("code", [])
        if len(codes) != 1 or not codes[0]:
            raise CfeError("Code d'autorisation mobile absent.")
        return codes[0]


def utc_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


class MobileSession:
    def __init__(
        self, path: Path | None = None, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.path = path or data_dir() / "mobile-session.json"
        self.http = httpx.AsyncClient(timeout=30, follow_redirects=False, transport=transport)
        self.keys: jwt.PyJWKSet | None = None

    async def __aenter__(self) -> MobileSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.http.aclose()

    @asynccontextmanager
    async def _guard(self):
        # Serialize refresh rotation across separate CLI and MCP processes.
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path.with_suffix(".lock"), flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            deadline = time.monotonic() + 65
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CfeError("Une autre connexion mobile est en cours.") from None
                    await asyncio.sleep(0.1)
            yield
        finally:
            os.close(fd)

    async def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.http.request(method, url, **kwargs)
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise CfeError("Échec de communication OAuth CFE ; aucun renvoi automatique.") from None
        if not isinstance(payload, dict):
            raise CfeError("Réponse OAuth CFE invalide.")
        if response.status_code != 200:
            if payload.get("error") in {"invalid_grant", "interaction_required", "login_required"}:
                raise AuthenticationRequired(LOGIN_REQUIRED)
            raise CfeError(f"Requête OAuth CFE refusée (HTTP {response.status_code}).")
        return payload

    async def _verify(
        self, token: str, *, nonce: str | None = None, allow_expired: bool = False
    ) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                raise ValueError
            if self.keys is None:
                self.keys = jwt.PyJWKSet.from_dict(await self._json("GET", JWKS_URL))
            matches = [key for key in self.keys.keys if key.key_id == header.get("kid")]
            if len(matches) != 1:
                raise ValueError
            claims = jwt.decode(
                token,
                matches[0].key,
                algorithms=["RS256"],
                audience=CLIENT_ID,
                issuer=ISSUER,
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                    "verify_exp": not allow_expired,
                },
            )
            if nonce is not None and claims.get("nonce") != nonce:
                raise ValueError
            if not claims["sub"]:
                raise ValueError
            return claims
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise CfeError("Jeton d'identité mobile invalide ; session non enregistrée.") from None

    async def _accept(
        self,
        payload: dict[str, Any],
        *,
        nonce: str | None = None,
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        claims = await self._verify(payload.get("id_token", ""), nonce=nonce)
        if previous and claims["sub"] != previous["subject"]:
            raise CfeError("Le renouvellement OAuth a changé de compte ; session non enregistrée.")
        refresh = payload.get("refresh_token")
        if refresh is not None and (not isinstance(refresh, str) or not refresh):
            raise CfeError("Jeton de renouvellement mobile invalide.")
        expiry = payload.get("refresh_token_expires_in")
        refresh_expiry = previous.get("refresh_expires_at") if previous else None
        if refresh and isinstance(expiry, (int, float)) and 0 < expiry < 10 * 365 * 86400:
            refresh_expiry = time.time() + expiry
        elif refresh:
            refresh_expiry = None
        session = {
            "kind": "mobile_oauth",
            "id_token": payload["id_token"],
            "refresh_token": refresh or (previous or {}).get("refresh_token"),
            "subject": claims["sub"],
            "id_expires_at": claims["exp"],
            "refresh_expires_at": refresh_expiry,
            "obtained_at": time.time(),
        }
        write_private_json(self.path, session)
        return session

    async def exchange(self, authorization: Authorization, callback: str) -> dict[str, Any]:
        code = authorization.code(callback)
        async with self._guard():
            payload = await self._json(
                "POST",
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT_URI,
                    "code": code,
                    "code_verifier": authorization.verifier,
                    "scope": SCOPE,
                },
            )
            session = await self._accept(payload, nonce=authorization.nonce)
            return self._summary(session, refreshed=False)

    async def _refresh(
        self, previous: dict[str, Any], *, invalidate_on_rejection: bool = True
    ) -> dict[str, Any]:
        if not previous.get("refresh_token"):
            raise AuthenticationRequired(LOGIN_REQUIRED)
        try:
            payload = await self._json(
                "POST",
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                    "refresh_token": previous["refresh_token"],
                    "scope": SCOPE,
                },
            )
        except AuthenticationRequired:
            if invalidate_on_rejection:
                write_private_json(self.path, {"kind": "mobile_oauth", "reauth_required": True})
            raise
        return await self._accept(payload, previous=previous)

    async def import_capture(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Import an explicitly selected local capture; verify and renew before persisting."""
        async with self._guard():
            claims = await self._verify(payload.get("id_token", ""), allow_expired=True)
            previous = {"subject": claims["sub"], "refresh_token": payload.get("refresh_token")}
            session = await self._refresh(previous, invalidate_on_rejection=False)
            return self._summary(session, refreshed=True)

    async def _current_session(self, *, force_refresh: bool = False) -> tuple[dict[str, Any], bool]:
        async with self._guard():
            if not self.path.exists():
                raise AuthenticationRequired(LOGIN_REQUIRED)
            try:
                session = read_private_json(self.path)
                if session.get("reauth_required"):
                    raise AuthenticationRequired(LOGIN_REQUIRED)
                claims = await self._verify(session["id_token"], allow_expired=True)
                if claims["sub"] != session["subject"]:
                    raise ValueError
            except (ValueError, KeyError, TypeError, AttributeError):
                raise CfeError("Fichier de session mobile invalide. " + LOGIN_REQUIRED) from None
            refreshed = force_refresh or claims["exp"] <= time.time() + 30
            session["id_expires_at"] = claims["exp"]
            if refreshed:
                session = await self._refresh(session)
            return session, refreshed

    async def identity(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Internal account identity for the API client; never return this through MCP."""
        session, _ = await self._current_session(force_refresh=force_refresh)
        return {
            "id_token": session["id_token"],
            "subject": session["subject"],
            "expires_at": session["id_expires_at"],
        }

    async def status(self, *, force_refresh: bool = False) -> dict[str, Any]:
        session, refreshed = await self._current_session(force_refresh=force_refresh)
        return self._summary(session, refreshed=refreshed)

    @staticmethod
    def _summary(session: dict[str, Any], *, refreshed: bool) -> dict[str, Any]:
        return {
            "authenticated": True,
            "authentication": "mobile_oauth",
            "refreshed": refreshed,
            "refresh_available": bool(session.get("refresh_token")),
            "id_token_expires_at": utc_time(session["id_expires_at"]),
            "refresh_expires_at_announced": (
                utc_time(session["refresh_expires_at"])
                if session.get("refresh_expires_at")
                else None
            ),
            "account_api_checked": False,
        }
