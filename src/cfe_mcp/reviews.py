"""Account-bound local reexamination drafts, separate from new care submissions."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from .claims import (
    DOCUMENT_TYPES,
    MAX_ATTACHMENT_BYTES,
    ClaimManager,
    digest,
    identifier,
    private_folder,
    read_attachment,
    snapshot_attachment,
)
from .client import COMPOSE_PATH, ORIGIN, CfeClient, CfeError
from .documents import POLICY, delivery_document
from .mobile_api import MobileApiClient
from .review_delivery import MAX_FILE, MAX_MESSAGE, UNCERTAIN, ReviewPortal, review_message
from .storage import read_private_json, write_private_json

COMPOSE_URL = ORIGIN + COMPOSE_PATH
REVIEW_KEYS = {
    "beneficiary_id", "request_number", "statement_reference", "subject", "message", "care_location"
}
MAX_REVIEW_ATTACHMENTS = 10  # Local storage policy, not a verified portal upload limit.


def clean_text(value: object, name: str, limit: int, *, optional: bool = False) -> str:
    if not isinstance(value, str):
        raise CfeError(f"{name} : texte attendu.")
    value = value.replace("\r\n", "\n").strip()
    if (not optional and not value) or len(value) > limit:
        raise CfeError(f"{name} : texte requis, limité à {limit} caractères.")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise CfeError(f"{name} : caractères de contrôle interdits.")
    return value


def normalize_review(value: dict, account: dict) -> dict:
    if not isinstance(value, dict) or set(value) - REVIEW_KEYS:
        raise CfeError("Champs de réexamen inconnus.")
    beneficiary = value.get("beneficiary_id")
    if not isinstance(beneficiary, str) or beneficiary not in {
        person["id"] for person in account["beneficiaries"]
    }:
        raise CfeError("Ce bénéficiaire n'appartient pas au compte connecté.")
    number = clean_text(value.get("request_number"), "Référence de la demande", 80)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 /-]*", number):
        raise CfeError("Référence de demande CFE invalide.")
    location = value.get("care_location", "etr")
    if location not in {"etr", "fra"}:
        raise CfeError("care_location doit être etr ou fra.")
    subject = clean_text(value.get("subject"), "Objet", 200)
    if "\n" in subject or "\t" in subject:
        raise CfeError("L'objet doit tenir sur une seule ligne.")
    return {
        "beneficiary_id": beneficiary,
        "request_number": number,
        "statement_reference": clean_text(
            value.get("statement_reference", ""), "Référence du décompte", 120, optional=True
        ),
        "subject": subject,
        "message": clean_text(value.get("message"), "Message", 10000),
        "care_location": location,
    }


class ReviewManager:
    def __init__(self, client: MobileApiClient, *, portal=None):
        self.client = client
        self.portal_adapter = portal
        self.folder = private_folder(private_folder(client.root) / "reviews")

    @asynccontextmanager
    async def portal(self):
        if self.portal_adapter is not None:
            yield self.portal_adapter
        else:
            async with CfeClient(self.client.root / "browser-state.json") as client:
                yield ReviewPortal(client)

    @staticmethod
    def submission_key(draft: dict) -> str:
        # A second message for the same request/statement must be handled as a follow-up,
        # not accidentally recreated by changing the text, attachment name or local draft ID.
        return digest({
            "subject": draft["subject_id"], "owner": draft["owner"],
            "request": draft["input"]["request_number"].upper(),
            "statement": draft["input"]["statement_reference"].upper(),
        })

    def ledger(self) -> dict:
        path = self.folder / "submissions.json"
        value = read_private_json(path) if path.exists() else {}
        if not isinstance(value, dict) or any(not isinstance(row, dict) for row in value.values()):
            raise CfeError("Journal des envois de réexamen invalide ; aucun renvoi.")
        return value

    @asynccontextmanager
    async def guard(self):
        fd = os.open(
            self.folder / ".lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CfeError("Un réexamen est déjà en cours de modification.") from None
                    await asyncio.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def directory(self, draft_id: str) -> Path:
        path = self.folder / identifier(draft_id)
        if path.is_symlink():
            raise CfeError("Dossier de réexamen invalide.")
        return path

    def save(self, draft: dict) -> None:
        draft["updated_at"] = time.time()
        write_private_json(self.directory(draft["id"]) / "review.json", draft)

    def load(self, draft_id: str, account: dict) -> dict:
        try:
            draft = read_private_json(self.directory(draft_id) / "review.json")
            if (
                draft["version"] != 1 or draft["id"] != draft_id
                or draft["state"] not in {"draft", "sending", "uncertain", "submitted"}
            ):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise CfeError("Brouillon de réexamen absent ou invalide.") from None
        if draft["subject_id"] != account["subject"] or draft["owner"] != account["owner"]:
            raise CfeError("Ce réexamen appartient à une autre connexion CFE.")
        previous = self.ledger().get(self.submission_key(draft))
        if draft["state"] == "sending" or (
            draft["state"] == "draft" and previous and previous.get("draft_id") == draft_id
        ):
            draft["state"] = "uncertain"
            if previous and previous.get("remote_id"):
                draft["remote_id"] = str(UUID(previous["remote_id"]))
            self.save(draft)
        return draft

    def view(self, draft: dict) -> dict:
        return {
            "source": "local_mcp",
            "workflow": "reexamination",
            "draft_id": draft["id"],
            "revision": draft["revision"],
            "state": draft["state"],
            "created_at": draft["created_at"],
            "updated_at": draft["updated_at"],
            "review": draft["input"],
            "source_claim_id": draft.get("source_claim_id"),
            "document_policy": POLICY,
            "outbound_documents": draft.get("outbound_documents", []),
            "attachments": [
                {key: row[key] for key in ("id", "name", "type", "bytes", "sha256", "state")}
                for row in draft["attachments"]
            ],
            "delivery": {
                "channel": "portal_messaging",
                "url": COMPOSE_URL,
                "submitted": draft["state"] == "submitted",
                "remote_submission_supported": True,
                "remote_id": draft.get("remote_id"),
                "receipt": draft.get("receipt"),
                "last_stage": draft.get("last_stage"),
                "note": "Ce réexamen ne doit pas être soumis comme un nouveau soin. "
                "Après un envoi incertain, vérifier le portail avant toute nouvelle tentative.",
            },
            "local_limits": {
                "attachments": MAX_REVIEW_ATTACHMENTS,
                "attachment_bytes": MAX_ATTACHMENT_BYTES,
                "portal_limits_verified": False,
                "portal_message_characters": MAX_MESSAGE,
                "outbound_attachment_bytes": MAX_FILE,
            },
        }

    @staticmethod
    def check_revision(draft: dict, expected_revision: int) -> None:
        if type(expected_revision) is not int or draft["revision"] != expected_revision:
            raise CfeError("Le réexamen a changé ; relire sa révision avant modification.")

    @staticmethod
    def mutable(draft: dict) -> None:
        if draft["state"] != "draft":
            raise CfeError(
                "Ce réexamen est déjà envoyé ou en cours de transmission. "
                "Aucune modification ni nouvel envoi sans vérifier son état à la CFE."
            )

    def new(self, values: dict, account: dict, draft_id: str | None = None) -> dict:
        return {
            "version": 1,
            "id": draft_id or uuid4().hex,
            "revision": 1,
            "state": "draft",
            "subject_id": account["subject"],
            "owner": account["owner"],
            "created_at": time.time(),
            "input": normalize_review(values, account),
            "attachments": [],
        }

    async def prepare(self, values: dict) -> dict:
        async with self.guard():
            draft = self.new(values, await self.client.account_context())
            private_folder(self.directory(draft["id"]))
            self.save(draft)
            return self.view(draft)

    async def get(self, draft_id: str, refresh: bool = False) -> dict:
        async with self.guard():
            draft = self.load(draft_id, await self.client.account_context())
            if refresh and draft.get("remote_id"):
                async with self.portal() as portal:
                    receipt = await portal.verify(draft, self.directory(draft_id))
                draft.update(state="submitted", receipt=receipt, last_stage="verified")
                self.save(draft)
            return self.view(draft)

    async def list(self) -> dict:
        async with self.guard():
            account = await self.client.account_context()
            records = []
            for path in sorted(self.folder.iterdir()):
                if not re.fullmatch(r"[0-9a-f]{32}", path.name):
                    continue
                record = self.directory(path.name) / "review.json"
                if not record.exists():  # A recoverable interrupted local import.
                    continue
                draft = read_private_json(record)
                if (
                    draft.get("subject_id") != account["subject"]
                    or draft.get("owner") != account["owner"]
                ):
                    continue
                item = self.view(self.load(path.name, account))
                keys = ("draft_id", "revision", "state", "updated_at", "review")
                records.append({key: item[key] for key in keys})
            return {"source": "local_mcp", "workflow": "reexamination", "records": records}

    async def update(self, draft_id: str, expected_revision: int, values: dict) -> dict:
        async with self.guard():
            account = await self.client.account_context()
            draft = self.load(draft_id, account)
            self.mutable(draft)
            self.check_revision(draft, expected_revision)
            draft["input"] = normalize_review(values, account)
            draft["revision"] += 1
            self.save(draft)
            return self.view(draft)

    async def attach(self, draft_id: str, path: str, document_type: str) -> dict:
        if document_type not in DOCUMENT_TYPES:
            raise CfeError("Type de pièce attendu : feuille, ordonnance, facture ou autre.")
        async with self.guard():
            draft = self.load(draft_id, await self.client.account_context())
            self.mutable(draft)
            if len(draft["attachments"]) >= MAX_REVIEW_ATTACHMENTS:
                raise CfeError("Nombre maximal de pièces locales atteint.")
            attachment = await asyncio.to_thread(
                snapshot_attachment, path, self.directory(draft_id), uuid4().hex
            )
            if attachment["sha256"] in {row["sha256"] for row in draft["attachments"]}:
                (self.directory(draft_id) / attachment["blob"]).unlink()
                raise CfeError("Cette pièce est déjà présente dans le réexamen.")
            attachment["type"] = document_type
            draft["attachments"].append(attachment)
            draft["revision"] += 1
            self.save(draft)
            return self.view(draft)

    async def remove(self, draft_id: str, attachment_id: str) -> dict:
        async with self.guard():
            draft = self.load(draft_id, await self.client.account_context())
            self.mutable(draft)
            aid = identifier(attachment_id)
            retained = [row for row in draft["attachments"] if row["id"] != aid]
            if len(retained) == len(draft["attachments"]):
                raise CfeError("Pièce absente de ce réexamen.")
            draft["attachments"] = retained
            draft["revision"] += 1
            self.save(draft)  # Keep the original snapshot for local traceability.
            return self.view(draft)

    def content(self, draft: dict, attachment: dict) -> bytes:
        blob = attachment["blob"]
        if Path(blob).name != blob or not blob.startswith(identifier(attachment["id"]) + "."):
            raise CfeError("Chemin privé du justificatif invalide.")
        path = self.directory(draft["id"]) / blob
        if path.is_symlink():
            raise CfeError("Le justificatif privé ne doit pas être un lien symbolique.")
        content = read_attachment(path)
        if (
            len(content) != attachment["bytes"]
            or hashlib.sha256(content).hexdigest() != attachment["sha256"]
        ):
            raise CfeError("Le justificatif a changé depuis sa préparation.")
        return content

    async def checked(self, draft_id: str, account: dict) -> dict:
        draft = self.load(draft_id, account)
        if normalize_review(draft["input"], account) != draft["input"]:
            raise CfeError("Données de réexamen incohérentes.")
        if not 1 <= len(draft["attachments"]) <= MAX_REVIEW_ATTACHMENTS:
            raise CfeError("Joindre au moins une pièce au réexamen.")
        draft["outbound_documents"] = []
        for attachment in draft["attachments"]:
            if attachment["state"] != "local" or attachment["type"] not in DOCUMENT_TYPES:
                raise CfeError("État ou type de pièce de réexamen invalide.")
            content = await asyncio.to_thread(self.content, draft, attachment)
            _, document = await asyncio.to_thread(
                delivery_document, content, attachment, self.directory(draft_id)
            )
            draft["outbound_documents"].append(document)
        return draft

    async def validate(self, draft_id: str) -> dict:
        async with self.guard():
            draft = await self.checked(draft_id, await self.client.account_context())
            review_message(draft)
            if any(d["bytes"] > MAX_FILE for d in draft["outbound_documents"]):
                raise CfeError("Une copie nettoyée dépasse les 3 Mo retenus pour le portail.")
            return {**self.view(draft), "locally_valid": True, "checked_on_server": False}

    def reject_duplicate(self, draft: dict, account: dict, ledger: dict) -> None:
        key = self.submission_key(draft)
        if key in ledger:
            raise CfeError("Un réexamen de cette demande et de ce décompte existe ; aucun doublon.")
        # Include older supervised sends, which predate the shared ledger.
        for folder in self.folder.iterdir():
            if not re.fullmatch(r"[0-9a-f]{32}", folder.name) or folder.name == draft["id"]:
                continue
            path = self.directory(folder.name) / "review.json"
            if not path.exists():
                continue
            other = read_private_json(path)
            if (
                other.get("subject_id") == account["subject"]
                and other.get("owner") == account["owner"]
                and other.get("state") != "draft" and self.submission_key(other) == key
            ):
                raise CfeError("Un réexamen de cette demande existe déjà ; aucun doublon créé.")

    async def submit(self, draft_id: str, expected_revision: int) -> dict:
        async with self.guard():
            account = await self.client.account_context()
            draft = self.load(draft_id, account)
            self.check_revision(draft, expected_revision)
            if draft["state"] == "submitted":
                return self.view(draft)
            if draft["state"] != "draft":
                raise CfeError(UNCERTAIN)
            ledger = self.ledger()
            self.reject_duplicate(draft, account, ledger)
            draft = await self.checked(draft_id, account)
            review_message(draft)
            documents = []
            for attachment in draft["attachments"]:
                original = await asyncio.to_thread(self.content, draft, attachment)
                content, document = await asyncio.to_thread(
                    delivery_document, original, attachment, self.directory(draft_id)
                )
                if len(content) > MAX_FILE:
                    raise CfeError("Une copie nettoyée dépasse les 3 Mo retenus pour le portail.")
                documents.append((content, document))
            key = self.submission_key(draft)

            def checkpoint(stage, **values):
                if "remote_id" in values:
                    values["remote_id"] = str(UUID(values["remote_id"]))
                entry = {
                    **ledger.get(key, {}), "draft_id": draft_id, "stage": stage,
                    "remote_id": values.get("remote_id", draft.get("remote_id")),
                    "updated_at": time.time(),
                }
                # Reserve globally BEFORE the local intention and before the browser click.
                ledger[key] = entry
                write_private_json(self.folder / "submissions.json", ledger)
                draft.update(state="sending", last_stage=stage)
                if values.get("remote_id"):
                    draft["remote_id"] = values["remote_id"]
                draft.setdefault("operations", []).append({
                    "stage": stage, "time": time.time(), **values,
                })
                self.save(draft)

            try:
                async with self.portal() as portal:
                    draft.update(await portal.bind(draft))
                    self.save(draft)
                    draft["receipt"] = await portal.send(draft, documents, checkpoint)
                    if (
                        not draft.get("remote_id")
                        or draft["receipt"].get("request_id") != draft["remote_id"]
                    ):
                        raise CfeError("Accusé associé à un dossier CFE inattendu.")
                    checkpoint("acknowledged")
                    receipt = await portal.verify(draft, self.directory(draft_id))
                draft.update(state="submitted", receipt=receipt, last_stage="verified")
                self.save(draft)
            except BaseException as error:
                if key in ledger:
                    draft["state"] = "uncertain"
                    self.save(draft)
                if not isinstance(error, Exception):
                    raise
                if key in ledger:
                    raise CfeError(UNCERTAIN) from None
                if isinstance(error, CfeError):
                    raise
                raise CfeError(
                    "Parcours de réexamen indisponible ; aucun envoi effectué."
                ) from None
            return self.view(draft)

    async def import_claim(
        self, source_claim_id: str, expected_revision: int, values: dict
    ) -> dict:
        if type(expected_revision) is not int:
            raise CfeError("Révision du brouillon source incorrecte.")
        async with self.guard():
            account = await self.client.account_context()
            claims = ClaimManager(self.client)
            async with claims.guard():
                source = claims.load(source_claim_id, account)
                values = {**values, "beneficiary_id": source["input"]["beneficiary_id"]}
                clean = normalize_review(values, account)
                if source.get("review_only"):
                    if source.get("review_source_revision") != expected_revision:
                        raise CfeError("Révision du brouillon source incorrecte.")
                    draft_id = identifier(source["review_id"])
                    if (self.directory(draft_id) / "review.json").exists():
                        existing = self.load(draft_id, account)
                        if existing["input"] != clean:
                            raise CfeError("Le réexamen existe déjà ; utiliser sa mise à jour.")
                        return self.view(existing)
                else:
                    claims.mutable(source, expected_revision)
                    draft_id = uuid4().hex
                if source["state"] != "draft" or source["operations"] or any(
                    row["state"] != "local" for row in source["attachments"]
                ):
                    raise CfeError("Seul un brouillon sans écriture CFE peut devenir un réexamen.")
                if len(source["attachments"]) > MAX_REVIEW_ATTACHMENTS:
                    raise CfeError("Trop de pièces pour ce réexamen local.")
                for attachment in source["attachments"]:
                    await asyncio.to_thread(claims.content, source, attachment)
                if not source.get("review_only"):
                    # Persist the submission block before any import work. A crash can be retried
                    # with the same source revision; it must never leave the original sendable.
                    source.update(
                        review_only=True, review_id=draft_id,
                        review_source_revision=expected_revision,
                        revision=source["revision"] + 1,
                    )
                    claims.save(source)
                folder = private_folder(self.directory(draft_id))
                draft = self.new(clean, account, draft_id)
                draft["source_claim_id"] = source_claim_id
                for attachment in source["attachments"]:
                    copied = await asyncio.to_thread(
                        snapshot_attachment,
                        str(claims.directory(source_claim_id) / attachment["blob"]),
                        folder, uuid4().hex,
                    )
                    if copied["sha256"] != attachment["sha256"]:
                        raise CfeError("La pièce source a changé pendant son import.")
                    copied.update(name=attachment["name"], type=attachment["type"])
                    draft["attachments"].append(copied)
                self.save(draft)
                return self.view(draft)

    @staticmethod
    def export_file(path: Path, content: bytes) -> None:
        if path.is_symlink():
            raise CfeError("Un fichier d'export ne doit pas être un lien symbolique.")
        if path.exists():
            if path.read_bytes() != content:
                raise CfeError("L'export a été modifié ; aucune donnée existante n'a été écrasée.")
            return
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    async def export(self, draft_id: str, expected_revision: int) -> dict:
        async with self.guard():
            draft = await self.checked(draft_id, await self.client.account_context())
            self.check_revision(draft, expected_revision)
            folder = private_folder(
                self.directory(draft_id) / f"export-{draft['revision']}-{POLICY}"
            )
            values = draft["input"]
            message = f"Objet : {values['subject']}\n\n{values['message']}\n"
            self.export_file(folder / "message.txt", message.encode())
            pieces = []
            for index, attachment in enumerate(draft["attachments"], 1):
                name = attachment["name"]
                if Path(name).name != name or any(ord(char) < 32 for char in name):
                    raise CfeError("Nom de pièce invalide pour l'export.")
                content = await asyncio.to_thread(self.content, draft, attachment)
                content, document = await asyncio.to_thread(
                    delivery_document, content, attachment, self.directory(draft_id)
                )
                path = folder / f"{index:02d}-{document['name']}"
                self.export_file(path, content)
                pieces.append({**document, "path": str(path)})
            return {
                **self.view(draft), "locally_valid": True, "checked_on_server": False,
                "export_directory": str(folder), "message_path": str(folder / "message.txt"),
                "exported_attachments": pieces,
            }
