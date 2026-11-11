"""Account-scoped reimbursement bank changes through the observed portal form."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from .claims import digest, identifier, private_folder, read_attachment, snapshot_attachment
from .client import ORIGIN, AuthenticationRequired, CfeClient, CfeError, is_authenticated_html
from .documents import POLICY, delivery_document
from .storage import data_dir, read_private_json, write_private_json

BANK_PATH = "/coordonnees-bancaires/"
ADD_PATH = BANK_PATH + "modifier-remboursement/ajout/"
BANK_TITLE = "Compte bancaire remboursements"
CONFIRMATION = "Votre demande sera effective après validation et notification par la CFE."
UNCERTAIN = (
    "Changement bancaire incertain : aucun renvoi. Vérifier la démarche dans la CFE, "
    "puis relire le brouillon avec refresh=true."
)


def portal_owner(html: str) -> str:
    if not is_authenticated_html(html):
        raise AuthenticationRequired("Session du portail expirée ; utiliser cfe_start_login.")
    owners = set(re.findall(r"\bcontactId\s*:\s*['\"]([0-9a-fA-F-]{36})['\"]", html))
    if len(owners) != 1:
        raise CfeError("Identité du portail CFE introuvable ou ambiguë.")
    return str(UUID(owners.pop()))


def normalize_bank(holder: str, iban: str, country_id: str, bic: str = "") -> dict:
    holder = " ".join(holder.strip().upper().split())
    if not re.fullmatch(r"[A-Z ]{1,70}", holder):
        raise CfeError("Le titulaire doit contenir de 1 à 70 lettres et espaces, comme le portail.")
    iban = re.sub(r"\s+", "", iban).upper()
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}", iban):
        raise CfeError("Format IBAN invalide.")
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    if int(numeric) % 97 != 1:
        raise CfeError("Clé de contrôle IBAN invalide.")
    try:
        country_id = str(UUID(country_id))
    except (ValueError, AttributeError):
        raise CfeError("Choisir le country_id fourni par cfe_bank_details.") from None
    bic = bic.strip().upper()
    if bic and not re.fullmatch(r"[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?", bic):
        raise CfeError("Format BIC invalide.")
    return {"holder": holder, "iban": iban, "country_id": country_id, "bic": bic}


def partial_iban_length(iban_length: int) -> int:
    """The prefix is separate; spaces occur after every complete group except the last."""
    return iban_length - 2 + (iban_length - 1) // 4


def bank_display(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style"]):
        node.decompose()
    text = soup.get_text(" ", strip=True)
    result = {}
    for label, key in (
        ("remboursement", "reimbursement_iban"),
        ("cotisation", "contribution_iban"),
    ):
        match = re.search(rf"IBAN {label}\s*:\s*([A-Z]{{2}}[0-9A-Za-z*]+)", text)
        result[key] = match.group(1) if match else None
    return result


def countries(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find("select", id="new_ibanpayscompte")
    if select is None:
        raise CfeError("Le formulaire bancaire CFE a changé : liste des pays absente.")
    return [
        {"id": str(UUID(str(o["value"]))), "label": o.get_text(" ", strip=True)}
        for o in select.find_all("option")
        if o.get("value")
    ]


class BankBrowser:
    """Use portal cookies in an isolated browser, without changing authentication or permissions."""

    def __init__(self, client: CfeClient):
        self.client = client

    @asynccontextmanager
    async def context(self):
        channel = os.environ.get("CFE_BROWSER_CHANNEL", "chrome")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                channel=None if channel == "chromium" else channel, headless=True
            )
            context = await browser.new_context(
                storage_state={
                    "cookies": [
                        c
                        for c in self.client.state.get("cookies", [])
                        if c["domain"].lstrip(".") == "monespaceperso.cfe.fr"
                    ],
                    "origins": [],
                }
            )
            context.set_default_timeout(30000)
            try:
                yield context
            finally:
                # CfeClient persists these cookies on exit; do not let its old jar overwrite them.
                try:
                    cookies = [
                        c
                        for c in await context.cookies()
                        if c["domain"].lstrip(".") == "monespaceperso.cfe.fr"
                    ]
                    self.client.state = {"cookies": cookies, "origins": []}
                    self.client.http.cookies.clear()
                    for c in cookies:
                        self.client.http.cookies.set(
                            c["name"], c["value"], domain=c["domain"], path=c["path"]
                        )
                finally:
                    await browser.close()

    async def send(self, draft: dict, content: bytes, checkpoint) -> dict:
        bank, attachment = draft["bank"], draft["attachment"]
        async with self.context() as context:
            page = await context.new_page()
            await page.goto(ORIGIN + ADD_PATH, wait_until="domcontentloaded", timeout=60000)
            if portal_owner(await page.content()) != draft["owner"]:
                raise CfeError("La session du portail appartient à un autre compte.")
            if await page.locator("#EntityFormView_EntityID").input_value():
                raise CfeError("Un brouillon distant est déjà ouvert ; aucun écrasement effectué.")
            await page.locator("#new_titulaireducompte").fill(bank["holder"])
            await page.locator("#new_ibanpayscompte").select_option(bank["country_id"])
            checkpoint("creating")
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
                await page.locator("#NextButton").click()
            if portal_owner(await page.content()) != draft["owner"]:
                raise CfeError("Identité CFE inattendue après création du brouillon.")
            remote_id = str(UUID(await page.locator("#EntityFormView_EntityID").input_value()))
            checkpoint("created", remote_id=remote_id)
            config = await page.evaluate(
                "() => ({prefix: countryCode, length: nbCaractere, bicRequired: isBIC})"
            )
            if config["prefix"] != bank["iban"][:2] or config["length"] != len(bank["iban"]):
                raise CfeError("Le pays ou la longueur IBAN ne correspond pas au formulaire CFE.")
            if config["bicRequired"] and not bank["bic"]:
                raise CfeError("Un BIC est obligatoire pour ce pays.")
            await page.locator("#partial_iban").fill(bank["iban"][2:])
            if config["bicRequired"]:
                await page.locator("#new_biccompte").fill(bank["bic"])
            if await page.locator("#new_isdomprincipale_0").count():
                await page.locator("#new_isdomprincipale_0").check()
            # Only repair the observed off-by-one calculation. Keep checksum and all validators.
            expected = partial_iban_length(config["length"])
            current = await page.locator("#partial_iban").get_attribute("maxlength")
            legacy = config["length"] - 2 + config["length"] // 4
            if int(current or "0") not in {expected, legacy}:
                raise CfeError("Contrôle de longueur IBAN inattendu ; formulaire à vérifier.")
            await page.locator("#partial_iban").evaluate(
                "(element, length) => { element.maxLength = length; }", expected
            )
            if len(await page.locator("#partial_iban").input_value()) != expected:
                raise CfeError("Formatage du champ IBAN inattendu.")

            async with page.expect_event(
                "framenavigated",
                predicate=lambda f: (
                    urlsplit(f.url).netloc == "monespaceperso.cfe.fr"
                    and urlsplit(f.url).path.startswith("/_portal/modal-form-template-path/")
                ),
            ) as frame_event:
                await page.locator("#AjouterRIB a.create-action").click()
            frame = await frame_event.value
            params = parse_qs(urlsplit(frame.url).query)
            if params.get("refentity") != ["incident"] or params.get("refid") != [remote_id]:
                raise CfeError("Le justificatif n'est pas rattaché au brouillon bancaire courant.")
            mime = (
                "application/pdf"
                if attachment["name"].lower().endswith(".pdf")
                else ("image/png" if attachment["name"].lower().endswith(".png") else "image/jpeg")
            )
            checkpoint("uploading")
            await frame.locator("#AttachFile").set_input_files(
                {
                    "name": attachment["name"],
                    "mimeType": mime,
                    "buffer": content,
                }
            )
            await frame.wait_for_function(
                "() => document.querySelector('#InsertButton')?.disabled === false", timeout=60000
            )
            await frame.locator("#InsertButton").click()
            rows = page.locator("#AjouterRIB table tbody tr")
            await rows.filter(has_text=attachment["name"]).wait_for(timeout=60000)
            if await rows.count() != 1:
                raise CfeError("Nombre de justificatifs bancaires inattendu.")
            checkpoint("attached")
            if not await page.evaluate("Page_ClientValidate('')"):
                raise CfeError("Le formulaire CFE refuse les données ; vérifier le dossier.")
            if await page.locator("#new_ibannumerocompte").input_value() != bank["iban"]:
                raise CfeError("L'IBAN du formulaire diffère du brouillon.")
            yes = page.locator("#new_isdomprincipale_1")
            if await yes.count() and await yes.is_checked():
                raise CfeError("Ce parcours ne modifie que le compte des remboursements.")
            checkpoint("submitting")
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
                await page.locator("#NextButton").click()
            url = urlsplit(page.url)
            params = parse_qs(url.query)
            if (
                url.netloc != "monespaceperso.cfe.fr"
                or url.path != BANK_PATH
                or params.get("success") != ["true"]
                or params.get("type") != ["bancaire"]
                or CONFIRMATION not in await page.locator("body").inner_text()
            ):
                raise CfeError("Accusé bancaire CFE non confirmé.")
            if portal_owner(await page.content()) != draft["owner"]:
                raise CfeError("Identité CFE inattendue sur l'accusé bancaire.")
            return {
                "request_id": remote_id,
                "status": "Envoyée à la CFE",
                "activation": "pending_cfe_validation",
                "message": CONFIRMATION,
                "url": f"{ORIGIN}/mes-demarches/detail/?id={remote_id}",
            }


class BankManager:
    def __init__(self, client: CfeClient, browser=None):
        self.client = client
        self.folder = private_folder(data_dir() / "bank-changes")
        self.browser = browser or BankBrowser(client)

    @asynccontextmanager
    async def guard(self):
        fd = os.open(self.folder / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CfeError("Une opération bancaire est déjà en cours.") from None
                    await asyncio.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def directory(self, draft_id):
        path = self.folder / identifier(draft_id)
        if path.is_symlink():
            raise CfeError("Dossier bancaire local invalide.")
        return path

    def save(self, draft):
        draft["updated_at"] = time.time()
        write_private_json(self.directory(draft["id"]) / "draft.json", draft)

    def key(self, draft):
        return digest(
            {"owner": draft["owner"], "purpose": "reimbursements", "iban": draft["bank"]["iban"]}
        )

    def ledger(self):
        path = self.folder / "submissions.json"
        return read_private_json(path) if path.exists() else {}

    def load(self, draft_id, owner):
        draft = read_private_json(self.directory(draft_id) / "draft.json")
        if draft.get("version") != 1 or draft.get("id") != draft_id:
            raise CfeError("Brouillon bancaire invalide.")
        if draft["owner"] != owner:
            raise CfeError("Ce brouillon bancaire appartient à un autre compte.")
        previous = self.ledger().get(self.key(draft))
        if draft["state"] == "sending" or (
            draft["state"] == "draft" and previous and previous["draft_id"] == draft_id
        ):
            draft["state"] = "uncertain"
            self.save(draft)
        return draft

    def view(self, draft):
        iban = draft["bank"]["iban"]
        result = {
            "draft_id": draft["id"],
            "revision": draft["revision"],
            "state": draft["state"],
            "purpose": "reimbursements",
            "changes_contributions": False,
            "bank": {k: v for k, v in draft["bank"].items() if k != "iban"},
            "iban_masked": iban[:4] + "…" + iban[-4:],
            "iban_length": len(iban),
            "attachment": {k: draft["attachment"][k] for k in ("name", "bytes", "sha256")},
            "remote_id": draft.get("remote_id"),
            "receipt": draft.get("receipt"),
            "last_stage": draft.get("last_stage"),
            "document_policy": POLICY,
            "outbound_document": draft["attachment"].get("sent_document"),
        }
        if draft["state"] == "uncertain":
            result["message"] = UNCERTAIN
        return result

    async def requests(self):
        records, cookie = [], ""
        for page in range(1, 101):
            result = await self.client.grid("requests", page, 50, cookie)
            records.extend(
                r
                for r in result["records"]
                if r["fields"].get("title", {}).get("value") == BANK_TITLE
            )
            if not result.get("has_more"):
                return records
            cookie = result.get("next_paging_cookie") or ""
        raise CfeError("Historique bancaire incomplet ; aucun envoi effectué.")

    async def details(self):
        html = await self.client.page(BANK_PATH)
        owner = portal_owner(html)
        options = await self.client.page(ADD_PATH)
        if portal_owner(options) != owner:
            raise CfeError("Le compte CFE a changé pendant la lecture.")
        return {
            "source": "portal",
            **bank_display(html),
            "countries": countries(options),
            "requests": await self.requests(),
            "scope": "Changement du compte de remboursement uniquement.",
        }

    async def prepare(self, holder, iban, country_id, proof_path, bic=""):
        bank = normalize_bank(holder, iban, country_id, bic)
        async with self.guard():
            html = await self.client.page(ADD_PATH)
            owner = portal_owner(html)
            country = next((c for c in countries(html) if c["id"] == bank["country_id"]), None)
            if country is None:
                raise CfeError("Ce pays ne figure pas dans les options IBAN de la CFE.")
            bank["country_label"] = country["label"]
            draft_id = uuid4().hex
            folder = private_folder(self.directory(draft_id))
            attachment = snapshot_attachment(proof_path, folder, uuid4().hex)
            # The portal advertises 3 Mo, more conservative than the claims API's 3 MiB.
            if attachment["bytes"] > 3_000_000:
                raise CfeError("Le justificatif bancaire dépasse les 3 Mo du portail.")
            draft = {
                "version": 1,
                "id": draft_id,
                "revision": 1,
                "state": "draft",
                "owner": owner,
                "bank": bank,
                "attachment": attachment,
                "created_at": time.time(),
            }
            self.save(draft)
            return self.view(draft)

    async def get(self, draft_id, refresh=False):
        async with self.guard():
            owner = portal_owner(await self.client.page(BANK_PATH))
            draft = self.load(draft_id, owner)
            if refresh and draft.get("remote_id"):
                records = await self.requests()
                record = next((r for r in records if r["id"] == draft["remote_id"]), None)
                if record:
                    fields = record["fields"]
                    status = fields.get("new_statutportail", {}).get("formatted")
                    if status in {
                        "Envoyée à la CFE",
                        "En cours de traitement CFE",
                        "En attente retour client",
                        "Traitée",
                    }:
                        draft["state"] = "submitted"
                        draft["receipt"] = {
                            "request_id": record["id"],
                            "status": status,
                            "activation": "not_verified",
                            "message": CONFIRMATION,
                            "url": f"{ORIGIN}/mes-demarches/detail/?id={record['id']}",
                        }
                        self.save(draft)
            return self.view(draft)

    async def list(self):
        async with self.guard():
            owner = portal_owner(await self.client.page(BANK_PATH))
            records = []
            for folder in sorted(self.folder.iterdir()):
                if not re.fullmatch(r"[0-9a-f]{32}", folder.name):
                    continue
                path = self.directory(folder.name) / "draft.json"
                if path.is_file() and read_private_json(path).get("owner") == owner:
                    records.append(self.view(self.load(folder.name, owner)))
            return {"source": "local_mcp", "records": records}

    async def submit(self, draft_id, expected_revision):
        async with self.guard():
            owner = portal_owner(await self.client.page(BANK_PATH))
            draft = self.load(draft_id, owner)
            if expected_revision != draft["revision"]:
                raise CfeError("Révision différente ; relire le brouillon bancaire.")
            if draft["state"] == "submitted":
                return self.view(draft)
            if draft["state"] != "draft":
                raise CfeError(UNCERTAIN)
            ledger = self.ledger()
            key = self.key(draft)
            if key in ledger:
                raise CfeError("Un envoi de cet IBAN existe déjà ; aucun doublon créé.")
            for r in await self.requests():
                if r["fields"].get("statecode", {}).get("value") not in {1, 2}:
                    raise CfeError("Un changement bancaire est déjà en cours à la CFE.")
            att = draft["attachment"]
            blob = self.directory(draft_id) / att["blob"]
            if blob.parent != self.directory(draft_id) or blob.is_symlink():
                raise CfeError("Chemin de justificatif invalide.")
            content = read_attachment(blob)
            if len(content) != att["bytes"] or hashlib.sha256(content).hexdigest() != att["sha256"]:
                raise CfeError("Le justificatif a changé ; envoi interrompu.")
            content, document = await asyncio.to_thread(
                delivery_document, content, att, self.directory(draft_id)
            )
            if len(content) > 3_000_000:
                raise CfeError("La copie bancaire nettoyée dépasse les 3 Mo du portail.")
            att["sent_document"] = document
            outgoing = {**draft, "attachment": {**att, "name": document["name"]}}
            normalized = normalize_bank(
                **{k: draft["bank"][k] for k in ("holder", "iban", "country_id", "bic")}
            )
            if any(normalized[k] != draft["bank"][k] for k in normalized):
                raise CfeError("Données bancaires locales incohérentes.")

            def checkpoint(stage, **values):
                if key not in ledger:
                    ledger[key] = {"draft_id": draft_id, "started_at": time.time()}
                    write_private_json(self.folder / "submissions.json", ledger)
                draft.update(state="sending", last_stage=stage, **values)
                self.save(draft)

            try:
                receipt = await self.browser.send(outgoing, content, checkpoint)
                draft.update(state="submitted", receipt=receipt, last_stage="confirmed")
                self.save(draft)
            except BaseException as exc:
                if key in ledger:
                    draft["state"] = "uncertain"
                    self.save(draft)
                if not isinstance(exc, Exception):
                    raise
                if key in ledger:
                    raise CfeError(UNCERTAIN) from None
                if isinstance(exc, CfeError):
                    raise
                raise CfeError("Navigateur bancaire indisponible ; aucun envoi effectué.") from None
            return self.view(draft)
