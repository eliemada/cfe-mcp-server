"""Account-scoped CFE mobile actions, reproduced from the app and verified by reads."""

from __future__ import annotations

import asyncio
import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from .client import AuthenticationRequired, CfeError
from .mobile import MobileSession
from .storage import data_dir, read_private_json, write_private_json

API_BASE = "https://cfeprod.crm4.dynamics.com/api/data/v9.2/"
TECH_TOKEN_URL = (
    "https://login.microsoftonline.com/97b636b6-c27b-4241-bf4f-cd4b26885377/oauth2/v2.0/token"
)
TECH_SCOPE = "https://cfeprod.crm4.dynamics.com/.default"
READ_ACTIONS = frozenset(
    {
        "getRecipients",
        "getRemboursement",
        "getDocRemboursement",
        "getParams",
        "getCurrencies",
        "getActeDetails",
        "getCircumstances",
    }
)
WRITE_ACTIONS = frozenset({"checkCare", "addDocument", "deleteDocument", "sendRequest"})
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024


class MobileApiError(CfeError):
    def __init__(
        self, action: str, code: str = "", *, unauthorized: bool = False, rejected: bool = False
    ):
        safe_code = code if re.fullmatch(r"[\w.-]{1,60}", code) else "erreur"
        super().__init__(f"L'API CFE a refusé {action} ({safe_code or 'erreur'}).")
        self.unauthorized = unauthorized
        self.code = safe_code
        self.rejected = rejected


def decoded(value: Any) -> Any:
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            return json.loads(value)
        except ValueError:
            raise CfeError("Réponse JSON interne de la CFE invalide.") from None
    return value


def response_value(payload: dict[str, Any], action: str) -> Any:
    error = decoded(payload.get("error"))
    errors = error if isinstance(error, list) else [error]
    for item in errors:
        if not item:
            continue
        if not isinstance(item, dict):
            raise MobileApiError(action)
        code = str(item.get("errorType", item.get("code", "")))
        if code not in {"noError", ""} or item.get("errorText"):
            raise MobileApiError(
                action,
                code,
                unauthorized=code.casefold()
                in {
                    "tokenexpired",
                    "expiredtoken",
                    "invalidtoken",
                    "unauthorized",
                    "notauthenticated",
                },
                rejected=True,
            )
    if action in WRITE_ACTIONS:
        # Write callbacks in 3.7.0 use the explicit error channel, often a JSON list.
        # Missing error or an unexpected shape is NOT an acknowledgement.
        valid_empty = error == {} or error == []
        valid_success = isinstance(error, dict) and error.get("errorType") == "noError"
        valid_list = isinstance(error, list) and all(
            isinstance(item, dict) and item.get("errorType") == "noError" for item in error
        )
        if "error" not in payload or not (valid_empty or valid_success or valid_list):
            raise CfeError(f"Accusé CFE absent ou ambigu pour {action} ; ne pas renvoyer.")
        value = decoded(payload.get("responses"))
        if action == "addDocument":
            if not isinstance(value, dict) or not all(
                isinstance(value.get(k), str) and 0 < len(value[k]) <= 256
                for k in ("ref_id", "file_id")
            ):
                raise CfeError("Accusé d'ajout de document incomplet ; ne pas renvoyer.")
        return {"accepted": True, "response": value}
    if "responses" not in payload:
        raise CfeError(f"Réponse CFE incomplète pour {action}.")
    return decoded(payload["responses"])


def flatten_care_types(nodes: list[dict[str, Any]], parents: tuple[str, ...] = ()) -> list[dict]:
    leaves = []
    for node in nodes:
        labels = (*parents, str(node["label"]).strip())
        if node.get("details"):
            leaves.extend(flatten_care_types(node["details"], labels))
        else:
            leaves.append({"code": node["code"], "label": labels[-1], "path": list(labels)})
    return leaves


def save_pdf(root: Path, content: bytes, digest: str) -> Path:
    folder = root / "downloads"
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    target = folder / f"decompte-{digest[:20]}.pdf"
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=folder)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


class MobileApiClient:
    def __init__(
        self, root: Path | None = None, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.root = root or data_dir()
        self.http = httpx.AsyncClient(timeout=40, follow_redirects=False, transport=transport)
        self.identity = MobileSession(self.root / "mobile-session.json", transport=transport)
        self._account: dict[str, Any] | None = None
        self._technical: dict[str, Any] | None = None

    async def __aenter__(self) -> MobileApiClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.http.aclose()
        await self.identity.__aexit__()

    @asynccontextmanager
    async def _guard(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(
            self.root / "mobile-api.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(fd, 0o600)
            deadline = time.monotonic() + 60
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CfeError(
                            "Une autre opération de connexion mobile est en cours."
                        ) from None
                    await asyncio.sleep(0.1)
            yield
        finally:
            os.close(fd)

    def _load(self, name: str) -> dict[str, Any]:
        path = self.root / name
        if not path.exists():
            return {}
        try:
            value = read_private_json(path)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, OSError):
            raise CfeError("Fichier de configuration ou de session mobile illisible.") from None

    def _config(self) -> dict[str, Any]:
        config = self._load("mobile-service-config.json")
        if not config:
            raise CfeError("Configuration technique de l'application CFE absente du dossier privé.")
        try:
            if (
                config["token_url"] != TECH_TOKEN_URL
                or config["scope"] != TECH_SCOPE
                or config["api_base"] != API_BASE
                or not isinstance(config["client_secret"], str)
                or len(config["client_secret"]) < 24
            ):
                raise ValueError
            UUID(config["client_id"])
        except (KeyError, ValueError, TypeError, AttributeError):
            raise CfeError(
                "Configuration technique CFE invalide ; aucun secret transmis."
            ) from None
        return config

    async def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self.http.post(url, **kwargs)
        except httpx.HTTPError:
            raise CfeError("Communication CFE interrompue ; aucun renvoi automatique.") from None
        if len(response.content) > MAX_DOCUMENT_BYTES * 2:
            raise CfeError("Réponse CFE trop volumineuse.")
        return response

    async def _raw_action(
        self, action: str, body: dict[str, Any], technical: dict[str, Any]
    ) -> Any:
        if action not in READ_ACTIONS | WRITE_ACTIONS | {"auth"}:
            raise CfeError("Action mobile non prise en charge.")
        response = await self._post(
            API_BASE + "new_api_applimob_" + action,
            headers={"Authorization": "Bearer " + technical["access_token"]},
            json=body,
        )
        if response.status_code != 200:
            raise MobileApiError(
                action,
                f"HTTP_{response.status_code}",
                unauthorized=response.status_code in {401, 403},
                rejected=response.status_code in {401, 403},
            )
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            raise CfeError("Réponse CFE invalide.") from None
        return response_value(payload, action)

    async def _ensure(self, *, force: bool = False) -> tuple[dict, dict]:
        async with self._guard():
            identity = await self.identity.identity()
            config = self._config()
            technical = self._load("mobile-service-session.json")
            if (
                force
                or technical.get("expires_at", 0) <= time.time() + 60
                or technical.get("client_id") != config["client_id"]
            ):
                response = await self._post(
                    TECH_TOKEN_URL,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": config["client_id"],
                        "client_secret": config["client_secret"],
                        "scope": TECH_SCOPE,
                    },
                )
                try:
                    value = response.json()
                    if response.status_code != 200 or not isinstance(
                        value.get("access_token"), str
                    ):
                        raise ValueError
                    duration = int(value["expires_in"])
                    if not 60 < duration <= 86400:
                        raise ValueError
                except (ValueError, KeyError, TypeError, AttributeError):
                    raise CfeError("Authentification technique CFE refusée ou invalide.") from None
                technical = {
                    "access_token": value["access_token"],
                    "expires_at": time.time() + duration,
                    "client_id": config["client_id"],
                }
                write_private_json(self.root / "mobile-service-session.json", technical)
            account = self._load("mobile-account-session.json")
            if (
                force
                or account.get("expires_at", 0) <= time.time() + 30
                or account.get("subject") != identity["subject"]
            ):
                value = await self._raw_action(
                    "auth", {"id_token": identity["id_token"]}, technical
                )
                if not isinstance(value, dict) or value.get("access") is not True:
                    raise CfeError(
                        "La CFE n'a pas accordé l'accès à l'espace mobile de cet adhérent."
                    )
                peridt = str(value.get("peridt", ""))
                if not peridt.isascii() or not peridt.isdecimal() or int(peridt) <= 0:
                    raise CfeError("Identifiant adhérent absent de la réponse CFE.")
                if not isinstance(value.get("token"), str) or not value["token"]:
                    raise AuthenticationRequired(
                        "Jeton de compte CFE absent ; relancer la connexion."
                    )
                account = {
                    "token": value["token"],
                    "peridt": peridt,
                    "subject": identity["subject"],
                    "obtained_at": time.time(),
                    "expires_at": identity["expires_at"],
                }
                write_private_json(self.root / "mobile-account-session.json", account)
            self._account, self._technical = account, technical
            return account, technical

    async def _read(self, action: str, extra: dict[str, Any] | None = None) -> Any:
        if action not in READ_ACTIONS:
            raise CfeError("Cette action n'est pas une consultation mobile prise en charge.")
        for attempt in range(2):
            account, technical = await self._ensure(force=attempt == 1)
            body = dict(extra or {})
            if action in {"getRecipients", "getRemboursement", "getDocRemboursement"}:
                body["id"] = account["peridt"]
            body["token"] = account["token"]
            try:
                return await self._raw_action(action, body, technical)
            except MobileApiError as error:
                if not error.unauthorized or attempt:
                    raise
        raise CfeError("Connexion mobile indisponible.")

    async def _recipients(self) -> dict[str, Any]:
        value = await self._read("getRecipients")
        if not isinstance(value, dict) or not self._account:
            raise CfeError("Liste des bénéficiaires CFE invalide.")
        if str(value.get("perIdt")) != self._account["peridt"]:
            raise CfeError("La réponse CFE ne correspond pas à l'adhérent connecté.")
        if not isinstance(value.get("contactsLies", []), list):
            raise CfeError("Liste des bénéficiaires liés invalide.")
        return value

    async def beneficiaries(self) -> dict[str, Any]:
        root = await self._recipients()
        records = []
        for index, person in enumerate([root, *root.get("contactsLies", [])]):
            records.append(
                {
                    "id": str(person["perIdt"]),
                    "name": " ".join(
                        str(person.get(k, "")).strip() for k in ("firstName", "lastName")
                    ),
                    "holder": index == 0,
                    "deposit_access_code": person.get("depotPortailFDSL"),
                    "coverage_end_date": person.get("dateEndDuty") or None,
                }
            )
        return {"source": "mobile_api", "beneficiaries": records}

    async def account_context(self) -> dict[str, Any]:
        """Internal binding for drafts. Contains no token and is never exposed as a tool."""
        people = await self.beneficiaries()
        assert self._account is not None
        return {
            "subject": self._account["subject"],
            "owner": self._account["peridt"],
            "beneficiaries": people["beneficiaries"],
        }

    async def write_action(self, action: str, extra: dict, *, subject: str, owner: str) -> dict:
        """Account-bound writes for the journaled draft manager; NEVER automatically retry."""
        allowed = {
            "checkCare": {"body"},
            "addDocument": {"type", "file_name", "file_content", "ref_id"},
            "deleteDocument": {"file_id"},
            "sendRequest": {"body", "careId"},
        }
        if action not in allowed or set(extra) != allowed[action]:
            raise CfeError("Action ou champs d'écriture CFE non pris en charge.")
        account, technical = await self._ensure()
        if account["subject"] != subject or account["peridt"] != owner:
            raise CfeError("Le compte connecté a changé ; opération interrompue.")
        body = {**extra, "token": account["token"]}
        if action in {"checkCare", "sendRequest"}:
            body["id"] = account["peridt"]
        return await self._raw_action(action, body, technical)

    async def session_status(self) -> dict[str, Any]:
        beneficiaries = await self.beneficiaries()
        return {
            "authenticated": True,
            "source": "mobile_api",
            "account_api_checked": True,
            "beneficiary_count": len(beneficiaries["beneficiaries"]),
        }

    async def _reimbursements(self) -> list[dict[str, Any]]:
        value = await self._read("getRemboursement")
        if not isinstance(value, dict) or not isinstance(value.get("docs"), list):
            raise CfeError("Liste des remboursements CFE invalide.")
        return value["docs"]

    async def reimbursements(self, page: int = 1, page_size: int = 10) -> dict[str, Any]:
        if not 1 <= page_size <= 100 or page < 1:
            raise CfeError("Pagination invalide : page >= 1 et taille entre 1 et 100.")
        docs = await self._reimbursements()
        start = (page - 1) * page_size
        return {
            "source": "mobile_api",
            "page": page,
            "page_size": page_size,
            "total": len(docs),
            "has_more": start + page_size < len(docs),
            "next_page": page + 1 if start + page_size < len(docs) else None,
            "records": [
                {
                    "id": str(doc["id"]),
                    "beneficiary": doc["benef"],
                    "amount": doc["montant"],
                    "date": doc["date"],
                    "paid": str(doc["ispayed"]).casefold() == "true",
                }
                for doc in docs[start : start + page_size]
            ],
        }

    async def catalog(self) -> dict[str, Any]:
        result = {}
        for key, action in (
            ("countries", "getCurrencies"),
            ("circumstances", "getCircumstances"),
            ("limits", "getParams"),
            ("care_types", "getActeDetails"),
        ):
            result[key] = await self._read(action)
        if not all(
            isinstance(result[k], list) for k in ("countries", "circumstances", "care_types")
        ):
            raise CfeError("Catalogue de remboursement CFE invalide.")
        if not isinstance(result["limits"], dict):
            raise CfeError("Limites de dépôt CFE invalides.")
        result["care_types"] = flatten_care_types(result["care_types"])
        return result

    async def claim_options(self, kind: str = "etr", country_search: str = "") -> dict[str, Any]:
        if kind not in {"etr", "fra"}:
            raise CfeError("Parcours de soins inconnu : choisir etr ou fra.")
        catalog = await self.catalog()
        suffix = "ETR" if kind == "etr" else "FR"
        countries = [
            c for c in catalog["countries"] if (c["countryCode"] == "100") == (kind == "fra")
        ]
        if country_search:
            countries = [
                c for c in countries if country_search.casefold() in c["countryName"].casefold()
            ]
        return {
            "source": "mobile_api",
            "kind": kind,
            **await self.beneficiaries(),
            "countries": countries,
            "circumstances": catalog["circumstances"],
            "limits": {
                "attachments": int(catalog["limits"][f"NB_PJ_MAX_{suffix}"]),
                "care_items": int(catalog["limits"][f"NB_SOINS_MAX_{suffix}"]),
            },
            "care_type_count": len(catalog["care_types"]),
        }

    async def care_types(self, search: str = "") -> dict[str, Any]:
        nodes = await self._read("getActeDetails")
        records = flatten_care_types(nodes)
        if search:
            terms = search.casefold().split()
            records = [
                r
                for r in records
                if all(term in " ".join([r["code"], *r["path"]]).casefold() for term in terms)
            ]
        return {"source": "mobile_api", "count": len(records), "records": records}

    async def download_statement(self, reimbursement_id: str) -> dict[str, Any]:
        docs = await self._reimbursements()
        if reimbursement_id not in {str(doc["id"]) for doc in docs}:
            raise CfeError("Ce décompte ne figure pas dans les remboursements du compte connecté.")
        value = await self._read("getDocRemboursement", {"idRemboursement": reimbursement_id})
        try:
            encoded = value["filecontent"]
            if not isinstance(encoded, str) or len(encoded) > MAX_DOCUMENT_BYTES * 1.4:
                raise ValueError
            content = base64.b64decode(encoded, validate=True)
            if not content.startswith(b"%PDF-") or len(content) > MAX_DOCUMENT_BYTES:
                raise ValueError
        except (ValueError, TypeError, KeyError, binascii.Error):
            raise CfeError(
                "Le décompte CFE n'est pas un PDF valide ou dépasse la taille acceptée."
            ) from None
        digest = hashlib.sha256(content).hexdigest()
        target = await asyncio.to_thread(save_pdf, self.root, content, digest)
        return {
            "source": "mobile_api",
            "reimbursement_id": reimbursement_id,
            "path": str(target),
            "bytes": len(content),
            "sha256": digest,
        }
