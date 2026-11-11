"""HTTP adapter for the Power Pages requests observed on the CFE portal."""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .storage import data_dir, read_private_json, write_private_json

ORIGIN = "https://monespaceperso.cfe.fr"
RELOGIN = "Utiliser cfe_start_login ou `cfe login`."
CLAIM_PATH = "/mes-remboursements/demande/depot/"
COMPOSE_PATH = "/messagerie/ecrire-message/"
ClaimKind = Literal["etr", "fra"]
GRID_PAGES = {
    "reimbursements": "/mes-remboursements/",
    "requests": "/mes-demarches/",
    "beneficiaries": "/mes-contrats/mes-beneficiaires/",
    "messages": "/messagerie/message-recu/",
}


class CfeError(Exception):
    """An actionable error safe to return to the MCP client."""


class AuthenticationRequired(CfeError):
    pass


def portal_url(path: str) -> str:
    """Do not send session credentials to a URL outside the configured portal."""
    if any(c in path for c in ("\\", "\r", "\n", "\x00")):
        raise CfeError("URL CFE invalide.")
    decoded = unquote(path)
    if "\\" in decoded or decoded.startswith("//"):
        raise CfeError("URL CFE invalide.")
    url = urlsplit(urljoin(ORIGIN, path))
    if url.scheme != "https" or url.netloc != "monespaceperso.cfe.fr":
        raise CfeError("Cette URL n'appartient pas au portail CFE.")
    if url.fragment:
        raise CfeError("URL CFE invalide.")
    return url.geturl()


def is_authenticated_html(html: str) -> bool:
    return bool(
        re.search(
            r"(?:var|let|const)\s+isPortalUserLoggedIn\s*=\s*(?:true|'True'|\"True\")\s*;",
            html,
        )
    )


def unwrap_value(value: Any) -> Any:
    if isinstance(value, dict):
        if "Value" in value:
            return unwrap_value(value["Value"])
        if "Id" in value:
            return {"id": value["Id"], "name": value.get("Name")}
    return value


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    fields = {}
    for attr in record.get("Attributes", []):
        metadata = attr.get("AttributeMetadata") or {}
        label = (metadata.get("DisplayName") or {}).get("UserLocalizedLabel") or {}
        fields[attr["Name"]] = {
            "label": label.get("Label", attr["Name"]),
            "value": unwrap_value(attr.get("Value")),
            "formatted": attr.get("FormattedValue"),
        }
    return {"id": record["Id"], "entity": record["EntityName"], "fields": fields}


def parse_claim_options(html: str, kind: ClaimKind) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    def options(field: str) -> list[dict[str, str]]:
        select = soup.find("select", id=field)
        if select is None:
            raise CfeError(f"Le formulaire CFE a changé : champ {field} absent.")
        return [
            {"id": str(o["value"]), "label": re.sub(r"\s+\d{13,15}\b", "", o.get_text()).strip()}
            for o in select.find_all("option")
            if o.get("value")
        ]

    # The page removes ineligible beneficiaries in JS using benefsFr_Etr.
    eligible = set(re.findall(r"benefsFr_Etr\.set\('([\w-]+)'\s*,\s*'(10000000[13])'\)", html))
    ids = {identifier for identifier, _ in eligible}
    beneficiaries = [o for o in options("new_contactbeneficiaire") if o["id"] in ids]
    countries = options("new_paysdesoins")
    countries = [c for c in countries if (c["label"].upper() == "FRANCE") == (kind == "fra")]
    limits = {}
    for var in ("nbPJMaxETR", "nbPJMaxFR", "nbSoinsMaxETR", "nbSoinsMaxFR"):
        match = re.search(rf"var\s+{var}\s*=\s*(\d+)", html)
        if match:
            limits[var] = int(match[1])
    suffix = "ETR" if kind == "etr" else "FR"
    entity_id = soup.find(id="EntityFormView_EntityID")
    return {
        "kind": kind,
        "beneficiaries": beneficiaries,
        "countries": countries,
        "circumstances": options("new_circonstance"),
        "limits": {
            "attachments": limits.get(f"nbPJMax{suffix}"),
            "care_items": limits.get(f"nbSoinsMax{suffix}"),
        },
        "existing_draft": bool(entity_id and entity_id.get("value")),
    }


def parse_messaging_options(html: str) -> dict[str, Any]:
    """Read the initial form and its observed theme mapping without executing JavaScript."""
    soup = BeautifulSoup(html, "html.parser")
    theme = soup.find("select", id="new_theme")
    forms = soup.find("select", id="new_formulaireportaildelincident")
    if theme is None or forms is None or soup.find(id="NextButton") is None:
        raise CfeError("Le formulaire de messagerie CFE a changé ou est absent.")
    form_labels = {
        str(option["value"]): option.get_text(strip=True)
        for option in forms.select("option[value]") if option["value"]
    }
    mapping = {}
    for script in soup.find_all("script"):
        code = script.get_text()
        if "#new_theme" not in code or "#new_formulaireportaildelincident" not in code:
            continue
        for _, code_id, form_id in re.findall(
            r"case\s+(['\"])(\d+)\1\s*:\s*formValue\s*=\s*(\d+)\s*;", code
        ):
            if code_id in mapping and mapping[code_id] != form_id:
                raise CfeError("Les options de messagerie CFE sont ambiguës.")
            mapping[code_id] = form_id
    themes = []
    for option in theme.select("option[value]"):
        code_id = str(option["value"])
        if not code_id:
            continue
        form_id = mapping.get(code_id)
        if form_id not in form_labels:
            raise CfeError("L'association des thèmes de messagerie CFE a changé.")
        themes.append({
            "id": code_id, "label": option.get_text(strip=True),
            "form_id": form_id, "form_label": form_labels[form_id],
        })
    review = next((row for row in themes if row["id"] == "100000000"), None)
    if review is None or review["form_id"] != "100000032":
        raise CfeError("Le parcours de réexamen CFE a changé.")
    entity = soup.find(id="EntityFormView_EntityID")
    return {
        "source": "portal", "url": ORIGIN + COMPOSE_PATH,
        "themes": themes, "reimbursement_review": review,
        "existing_remote_draft": bool(entity and entity.get("value")),
        "step": "theme_selection", "remote_submission_supported": True,
        "submission_tool": "cfe_submit_review", "supported_submission_themes": [review["id"]],
        "note": "Lecture de la première étape uniquement. Le bouton Suivant fait un postback "
        "susceptible de créer une démarche distante. Aucun postback effectué par cet outil. "
        "La soumission MCP est limitée au réexamen d'un remboursement existant.",
    }


class CfeClient:
    def __init__(
        self,
        state_path: Path | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.state_path = state_path or data_dir() / "browser-state.json"
        if not self.state_path.is_file():
            raise AuthenticationRequired("Session absente. " + RELOGIN)
        self.state = read_private_json(self.state_path)
        jar = httpx.Cookies()
        for cookie in self.state.get("cookies", []):
            if cookie["domain"].lstrip(".") == "monespaceperso.cfe.fr":
                jar.set(
                    cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"]
                )
        self.http = httpx.AsyncClient(
            base_url=ORIGIN,
            cookies=jar,
            timeout=httpx.Timeout(40, connect=15),
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": "cfe-mcp/0.1", "Accept-Language": "fr-FR,fr;q=0.9"},
        )
        self.token: str | None = None

    async def __aenter__(self) -> CfeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        # Preserve Set-Cookie renewals, including CSRF cookie rotation.
        original = {
            (c["name"], c["domain"], c["path"]): c
            for c in self.state.get("cookies", [])
            if c["domain"].lstrip(".") == "monespaceperso.cfe.fr"
        }
        cookies = []
        for c in self.http.cookies.jar:
            if c.domain.lstrip(".") != "monespaceperso.cfe.fr":
                continue
            old = original.get((c.name, c.domain, c.path), {})
            cookies.append(
                {
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path,
                    "expires": c.expires if c.expires is not None else old.get("expires", -1),
                    "httpOnly": c.has_nonstandard_attr("HttpOnly") or old.get("httpOnly", False),
                    "secure": c.secure or old.get("secure", True),
                    "sameSite": old.get("sameSite", "Lax"),
                }
            )
        try:
            write_private_json(self.state_path, {"cookies": cookies, "origins": []})
        finally:
            await self.http.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = portal_url(path)
        try:
            response = await self.http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            # Do not include HTTP headers, request bodies or server HTML in errors.
            raise CfeError("La requête CFE a échoué. Aucun renvoi automatique effectué.") from exc
        if response.status_code in {301, 302, 303, 307, 308, 401}:
            raise AuthenticationRequired("Session CFE expirée. " + RELOGIN)
        if response.status_code == 403:
            raise CfeError("Accès CFE refusé (403). Vérifier la session et les droits du compte.")
        if not response.is_success:
            raise CfeError(f"La CFE a répondu HTTP {response.status_code}.")
        return response

    async def page(self, path: str) -> str:
        response = await self.request("GET", path)
        if not is_authenticated_html(response.text):
            raise AuthenticationRequired("La page CFE n'est pas authentifiée. " + RELOGIN)
        return response.text

    async def csrf(self) -> str:
        response = await self.request("GET", "/_layout/tokenhtml")
        token = BeautifulSoup(response.text, "html.parser").find(
            "input", attrs={"name": "__RequestVerificationToken"}
        )
        if not token or not token.get("value"):
            raise AuthenticationRequired("Jeton de session CFE indisponible. " + RELOGIN)
        self.token = str(token["value"])
        return self.token

    async def session_status(self) -> dict[str, Any]:
        await self.page("/")
        return {"authenticated": True, "portal": ORIGIN}

    async def grid(
        self, collection: str, page: int = 1, page_size: int = 10, paging_cookie: str = ""
    ) -> dict[str, Any]:
        if collection not in GRID_PAGES:
            raise CfeError("Liste CFE inconnue.")
        if page < 1 or not 1 <= page_size <= 50:
            raise CfeError("page doit être >= 1 et page_size compris entre 1 et 50.")
        html = await self.page(GRID_PAGES[collection])
        grid = BeautifulSoup(html, "html.parser").select_one(".entity-grid[data-view-layouts]")
        if grid is None:
            raise CfeError("Le tableau CFE attendu est absent.")
        try:
            layouts = json.loads(base64.b64decode(str(grid["data-view-layouts"])))
            layout = next(x for x in layouts if x["Id"] == grid["data-selected-view"])
            secure_config = layout["Base64SecureConfiguration"]
            path = str(grid["data-get-url"])
        except (KeyError, ValueError, StopIteration) as exc:
            raise CfeError("Le format du tableau CFE a changé.") from exc
        # The server-issued signed configuration carries the account's permissions.
        offset = datetime.now().astimezone().utcoffset()
        payload = {
            "base64SecureConfiguration": secure_config,
            "sortExpression": layout.get("SortExpression", ""),
            "search": None,
            "page": page,
            "pageSize": page_size,
            "pagingCookie": paging_cookie,
            "filter": None,
            "metaFilter": None,
            "nlSearchFilter": "",
            "timezoneOffset": -int(offset.total_seconds() / 60) if offset else 0,
            "customParameters": [],
        }
        token = await self.csrf()
        response = await self.request(
            "POST",
            path,
            json=payload,
            headers={
                "__RequestVerificationToken": token,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            },
        )
        try:
            result = response.json()
            records = [normalize_record(r) for r in result["Records"]]
        except (ValueError, KeyError, TypeError) as exc:
            raise CfeError("Réponse de liste CFE inattendue.") from exc
        return {
            "records": records,
            "page": result.get("PageNumber", page),
            "page_size": result.get("PageSize", page_size),
            "total": result.get("ItemCount"),
            "has_more": result.get("MoreRecords", False),
            "next_paging_cookie": result.get("NextPagePagingCookie"),
        }

    async def claim_options(self, kind: ClaimKind = "etr") -> dict[str, Any]:
        if kind not in {"etr", "fra"}:
            raise CfeError("kind doit être etr (étranger) ou fra (France).")
        return parse_claim_options(await self.page(f"{CLAIM_PATH}?form={kind}"), kind)

    async def messaging_options(self) -> dict[str, Any]:
        return parse_messaging_options(await self.page(COMPOSE_PATH))
