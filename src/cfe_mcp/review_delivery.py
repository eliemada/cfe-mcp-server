"""Observed portal review form and account-scoped, read-only delivery verification."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from bs4 import BeautifulSoup
from pypdf import PdfReader

from .banking import BankBrowser, portal_owner
from .claims import private_folder
from .client import COMPOSE_PATH, ORIGIN, CfeClient, CfeError
from .documents import POLICY

THEME = "100000000"
FORM = "100000032"
CONFIRMATION = "Votre message a bien été pris en compte."
MAX_MESSAGE = 2500
MAX_FILE = 3_000_000
STATUSES = {
    "Envoyée à la CFE", "En cours de traitement CFE", "En attente retour client", "Traitée"
}
UNCERTAIN = (
    "Envoi du réexamen incertain : aucun renvoi. Relire avec refresh=true pour vérifier "
    "le dossier existant à la CFE."
)


def review_message(draft: dict) -> str:
    values = draft["input"]
    message = f"Objet : {values['subject']}\n\n{values['message']}"
    # HTML maxlength counts UTF-16 code units, including two for astral characters.
    if len(message.encode("utf-16-le")) // 2 > MAX_MESSAGE:
        raise CfeError("Objet et message réunis dépassent les 2 500 caractères du portail.")
    return message


def review_identity(html: str, draft: dict) -> str:
    owner = portal_owner(html)
    subjects = set(re.findall(r"\buserName\s*:\s*['\"]([^'\"]{1,128})['\"]", html))
    if subjects != {draft["subject_id"]}:
        raise CfeError("Les connexions mobile et portail appartiennent à des comptes différents.")
    if draft.get("portal_owner") and draft["portal_owner"] != owner:
        raise CfeError("La session du portail appartient à un autre compte.")
    return owner


def pdf_receipt_matches(content: bytes, reference: str, message: str) -> bool:
    """Read the CFE-generated summary; no OCR or alteration of a doctor's document."""
    try:
        pdf = PdfReader(io.BytesIO(content), strict=True)
        if pdf.is_encrypted or not 1 <= len(pdf.pages) <= 10:
            return False
        texts = []
        for page in pdf.pages:
            stream = page.get_contents()
            if stream and len(stream.get_data()) > 8 * 1024 * 1024:
                return False
            texts.append(page.extract_text())
        text = "\n".join(texts)
    except Exception:
        return False

    def normal(value):
        return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()

    return reference in text and normal(message) in normal(text)


def save_receipt(folder: Path, name: str, content: bytes) -> str:
    if not re.fullmatch(r"WEB-CAS-[A-Z0-9-]+\.pdf", name):
        raise CfeError("Nom du récapitulatif CFE inattendu.")
    target = private_folder(folder / "receipts") / name
    if target.is_symlink():
        raise CfeError("Chemin du récapitulatif CFE invalide.")
    if target.exists():
        if target.read_bytes() != content:
            raise CfeError("Le récapitulatif CFE a changé ; copie précédente conservée.")
    else:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    return str(target)


class ReviewBrowser(BankBrowser):
    """Reuse the isolated portal browser and its cookie persistence, not banking actions."""

    async def send(self, draft: dict, documents: list[tuple[bytes, dict]], checkpoint) -> dict:
        message = review_message(draft)
        for content, doc in documents:
            if (
                doc["policy"] != POLICY or len(content) != doc["bytes"]
                or hashlib.sha256(content).hexdigest() != doc["sha256"]
                or len(content) > MAX_FILE
            ):
                raise CfeError("Justificatif nettoyé invalide ; aucun envoi.")
        async with self.context() as context:
            page = await context.new_page()
            await page.goto(ORIGIN + COMPOSE_PATH, wait_until="domcontentloaded", timeout=60000)
            review_identity(await page.content(), draft)
            if await page.locator("#EntityFormView_EntityID").input_value():
                raise CfeError("Un dossier distant est déjà ouvert ; aucune nouvelle création.")
            await page.locator("#new_theme").select_option(THEME)
            if await page.locator("#new_formulaireportaildelincident").input_value() != FORM:
                raise CfeError("Le formulaire CFE de réexamen a changé.")
            if not await page.evaluate("Page_ClientValidate('')"):
                raise CfeError("Le portail refuse le thème du réexamen.")
            checkpoint("creating")
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
                await page.locator("#NextButton").click()
            review_identity(await page.content(), draft)
            remote_id = str(UUID(await page.locator("#EntityFormView_EntityID").input_value()))
            checkpoint("created", remote_id=remote_id)
            description = page.locator("#description")
            if await description.get_attribute("maxlength") != str(MAX_MESSAGE):
                raise CfeError("La limite de message CFE a changé ; vérifier le formulaire.")
            await description.fill(message)
            rows = page.locator("#Subgrid_PJ table tbody tr[data-id]")
            for index, (content, doc) in enumerate(documents, 1):
                review_identity(await page.content(), draft)
                async with page.expect_event(
                    "framenavigated",
                    predicate=lambda f: (
                        urlsplit(f.url).netloc == "monespaceperso.cfe.fr"
                        and urlsplit(f.url).path.startswith("/_portal/modal-form-template-path/")
                    ),
                ) as event:
                    await page.locator("#Subgrid_PJ a.create-action").click()
                frame = await event.value
                params = parse_qs(urlsplit(frame.url).query)
                if params.get("refentity") != ["incident"] or params.get("refid") != [remote_id]:
                    raise CfeError("La pièce n'est pas rattachée au réexamen courant.")
                suffix = Path(doc["name"]).suffix.lower()
                mime = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".png": "image/png"}
                if suffix not in mime:
                    raise CfeError("Format de copie nettoyée inattendu.")
                checkpoint("uploading", document=doc)
                await frame.locator("#AttachFile").set_input_files(
                    {"name": doc["name"], "mimeType": mime[suffix], "buffer": content}
                )
                await frame.wait_for_function(
                    "() => document.querySelector('#InsertButton')?.disabled === false",
                    timeout=60000,
                )
                await frame.locator("#InsertButton").click()
                await rows.filter(has_text=doc["name"]).wait_for(timeout=60000)
                if await rows.count() != index:
                    raise CfeError("Nombre de pièces jointes inattendu ; vérifier le dossier.")
                checkpoint("attached", document=doc)
            valid = await page.evaluate("Page_ClientValidate('')")
            if (
                await description.input_value() != message
                or await page.locator("#new_theme").input_value() != THEME
                or await page.locator("#EntityFormView_EntityID").input_value() != remote_id
                or not valid
            ):
                raise CfeError("Le formulaire ne correspond plus au réexamen validé.")
            review_identity(await page.content(), draft)
            for _, doc in documents:
                if await rows.filter(has_text=doc["name"]).count() != 1:
                    raise CfeError("Une pièce manque au réexamen courant.")
            acknowledged = False

            def on_response(response):
                nonlocal acknowledged
                url = urlsplit(response.url)
                params = parse_qs(url.query)
                if (
                    response.request.is_navigation_request()
                    and response.request.frame == page.main_frame
                    and url.netloc == "monespaceperso.cfe.fr" and url.path == COMPOSE_PATH
                    and response.status == 200 and params.get("success") == ["true"]
                    and params.get("type") == ["messagerie"]
                ):
                    acknowledged = True

            page.on("response", on_response)
            checkpoint("submitting")
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
                await page.locator(".send-btn:visible").click()
            review_identity(await page.content(), draft)
            success = page.locator("#success-message")
            if (
                not acknowledged or not await success.is_visible()
                or CONFIRMATION not in await success.inner_text()
            ):
                raise CfeError("Accusé de réception CFE non confirmé.")
            return {
                "request_id": remote_id, "message": CONFIRMATION,
                "submitted_at": time.time(), "document_count": len(documents),
                "url": f"{ORIGIN}/mes-demarches/detail/?id={remote_id}",
            }


class ReviewPortal:
    def __init__(self, client: CfeClient, browser=None):
        self.client = client
        self.browser = browser or ReviewBrowser(client)

    async def records(self) -> list[dict]:
        records, cookie = [], ""
        for page in range(1, 101):
            result = await self.client.grid("requests", page, 50, cookie)
            records.extend(result["records"])
            if not result.get("has_more"):
                return records
            next_cookie = result.get("next_paging_cookie")
            if not next_cookie or next_cookie == cookie:
                break
            cookie = next_cookie
        raise CfeError("Historique CFE incomplet ; vérifier la référence avant l'envoi.")

    async def bind(self, draft: dict) -> dict:
        owner = review_identity(await self.client.page(COMPOSE_PATH), draft)
        records = await self.records()
        original = [r for r in records if any(
            r["fields"].get(key, {}).get("value") == draft["input"]["request_number"]
            for key in ("new_numdemandegds", "ticketnumber")
        )]
        if len(original) != 1:
            raise CfeError("La demande à réexaminer est absente ou ambiguë dans ce compte.")
        return {"portal_owner": owner, "original_request_id": str(UUID(original[0]["id"]))}

    async def send(self, draft: dict, documents: list[tuple[bytes, dict]], checkpoint) -> dict:
        return await self.browser.send(draft, documents, checkpoint)

    async def annotation(self, link, csrf: str) -> bytes:
        identifier = str(UUID(link["data-value"]))
        response = await self.client.request(
            "GET", f"/_api/annotations({identifier})?$select=documentbody,filename,mimetype",
            headers={"__RequestVerificationToken": csrf, "Accept": "application/json"},
        )
        try:
            value = response.json()
            if value["filename"] != link.get_text(strip=True):
                raise ValueError
            encoded = value["documentbody"]
            if len(encoded) > 4 * MAX_FILE // 3 + 8:
                raise ValueError
            content = base64.b64decode(encoded, validate=True)
            if not content or len(content) > MAX_FILE:
                raise ValueError
            return content
        except (ValueError, KeyError, TypeError, binascii.Error):
            raise CfeError("Justificatif reçu de la CFE invalide ou trop volumineux.") from None

    async def verify(self, draft: dict, folder: Path) -> dict:
        remote_id = str(UUID(draft["remote_id"]))
        html = await self.client.page(f"/mes-demarches/detail/?id={remote_id}")
        review_identity(html, draft)
        record = next((r for r in await self.records() if r["id"] == remote_id), None)
        if not record or record["fields"].get("title", {}).get("value") != "Message à la CFE":
            raise CfeError("Le réexamen créé n'est pas encore visible dans l'historique CFE.")
        status = record["fields"].get("new_statutportail", {}).get("formatted")
        if status not in STATUSES:
            raise CfeError("La CFE ne confirme pas encore le statut d'envoi du réexamen.")
        links = BeautifulSoup(html, "html.parser").select("a.file-link")
        csrf = await self.client.csrf()
        documents = draft.get("outbound_documents", [])
        if len(documents) != len(draft["attachments"]) or not documents:
            raise CfeError("Empreintes des pièces envoyées absentes ; aucun accusé déduit.")
        for doc in documents:
            matches = [link for link in links if link.get_text(strip=True) == doc["name"]]
            if len(matches) != 1 or doc.get("policy") != POLICY:
                raise CfeError("Une pièce envoyée est absente ou ambiguë dans le réexamen CFE.")
            content = await self.annotation(matches[0], csrf)
            if len(content) != doc["bytes"] or hashlib.sha256(content).hexdigest() != doc["sha256"]:
                raise CfeError("Une pièce reçue par la CFE diffère de la copie nettoyée.")
        summaries = [link for link in links if (
            re.fullmatch(r"WEB-CAS-[A-Z0-9-]+\.pdf", link.get_text(strip=True))
            and link.get("data-file-id") == "" and "remote" not in link.get("class", [])
        )]
        if len(summaries) != 1:
            raise CfeError("Récapitulatif officiel du message absent ou ambigu.")
        name = summaries[0].get_text(strip=True)
        content = await self.annotation(summaries[0], csrf)
        if not content.startswith(b"%PDF-") or not await asyncio.to_thread(
            pdf_receipt_matches, content, name[:-4], review_message(draft)
        ):
            raise CfeError("Le récapitulatif CFE ne confirme pas le texte du réexamen.")
        path = await asyncio.to_thread(save_receipt, folder, name, content)
        return {
            **(draft.get("receipt") or {}), "request_id": remote_id, "status": status,
            "reference": name[:-4], "original_request_number": draft["input"]["request_number"],
            "message": CONFIRMATION, "document_count": len(documents),
            "server_documents_verified": True, "server_message_verified": True,
            "verified_at": time.time(), "official_pdf_path": path,
            "official_pdf_sha256": hashlib.sha256(content).hexdigest(),
            "url": f"{ORIGIN}/mes-demarches/detail/?id={remote_id}",
        }
