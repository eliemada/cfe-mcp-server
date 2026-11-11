"""Private drafts and a durable journal around account-scoped CFE writes."""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import os
import re
import stat
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from .care import care_schema, normalize_claim
from .client import CfeError
from .documents import POLICY, delivery_document
from .mobile_api import MobileApiClient, MobileApiError
from .storage import read_private_json, write_private_json

MAX_ATTACHMENT_BYTES = 3 * 1024 * 1024
DOCUMENT_TYPES = {
    "feuille": "100000000",
    "ordonnance": "100000001",
    "facture": "100000002",
    "autre": "100000003",
}
UNCERTAIN = (
    "Une écriture CFE a un résultat incertain. Aucun renvoi automatique : "
    "vérifier le dossier dans l'espace CFE avant toute nouvelle tentative."
)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise CfeError("Identifiant local de dossier ou de pièce invalide.")
    return value


def private_folder(path: Path) -> Path:
    if path.is_symlink():
        raise CfeError("Le dossier privé ne doit pas être un lien symbolique.")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)
    return path


def read_attachment(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CfeError("Le justificatif doit être un fichier ordinaire.")
            content = stream.read(MAX_ATTACHMENT_BYTES + 1)
    except OSError:
        raise CfeError("Justificatif local absent ou illisible.") from None
    if not content or len(content) > MAX_ATTACHMENT_BYTES:
        raise CfeError("Chaque justificatif doit être non vide et ne pas dépasser 3 Mio.")
    extension = path.suffix.lower()
    valid = (
        extension == ".pdf"
        and content.startswith(b"%PDF-")
        or extension in {".jpg", ".jpeg"}
        and content.startswith(b"\xff\xd8\xff")
        or extension == ".png"
        and content.startswith(b"\x89PNG\r\n\x1a\n")
    )
    if not valid:
        raise CfeError("Formats acceptés : PDF, JPEG et PNG, avec une extension cohérente.")
    return content


def snapshot_attachment(source: str, folder: Path, attachment_id: str) -> dict:
    path = Path(source).expanduser()
    if not path.is_absolute():
        raise CfeError("Fournir le chemin absolu du justificatif.")
    if any(ord(char) < 32 for char in path.name) or len(path.name) > 200:
        raise CfeError("Nom de justificatif trop long ou contenant des caractères de contrôle.")
    content = read_attachment(path)
    blob = attachment_id + path.suffix.lower()
    fd = os.open(folder / blob, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "id": attachment_id,
        "name": path.name,
        "blob": blob,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "state": "local",
    }


class ClaimManager:
    def __init__(self, client: MobileApiClient):
        self.client = client
        self.folder = private_folder(private_folder(client.root) / "claims")

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
                        raise CfeError("Une opération de dossier est déjà en cours.") from None
                    await asyncio.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def directory(self, draft_id: str) -> Path:
        path = self.folder / identifier(draft_id)
        if path.is_symlink():
            raise CfeError("Dossier local invalide.")
        return path

    def save(self, draft: dict) -> None:
        draft["updated_at"] = time.time()
        write_private_json(self.directory(draft["id"]) / "draft.json", draft)

    def load(self, draft_id: str, account: dict) -> dict:
        try:
            draft = read_private_json(self.directory(draft_id) / "draft.json")
            if not isinstance(draft, dict) or draft["version"] != 1 or draft["id"] != draft_id:
                raise ValueError
        except (ValueError, OSError, KeyError):
            raise CfeError("Dossier local absent ou illisible.") from None
        if draft["subject"] != account["subject"] or draft["owner"] != account["owner"]:
            raise CfeError("Ce dossier appartient à une autre connexion CFE.")
        if draft["state"] in {"uploading", "submitting", "deleting"}:
            draft["state"] = "uncertain"
            self.save(draft)
        # A crash can occur between writing the global ledger and the per-draft intent.
        # Surface that ambiguity when reading the draft, not only on a later submit call.
        ledger_path = self.folder / "submissions.json"
        if draft["state"] not in {"submitted", "uncertain"} and ledger_path.exists():
            ledger = read_private_json(ledger_path)
            previous = ledger.get(self.submission_key(draft))
            if previous and previous["draft_id"] == draft_id:
                draft["state"] = "uncertain"
                self.save(draft)
        return draft

    def view(self, draft: dict) -> dict:
        result = {
            "source": "local_mcp",
            "draft_id": draft["id"],
            "revision": draft["revision"],
            "state": draft["state"],
            "created_at": draft["created_at"],
            "updated_at": draft["updated_at"],
            "claim": draft["input"],
            "limits": draft["limits"],
            "attachments": [
                {k: row[k] for k in ("id", "name", "type", "bytes", "sha256", "state")}
                for row in draft["attachments"]
            ],
            "server_validation": draft.get("server_validation"),
            "receipt": draft.get("receipt"),
            "document_policy": POLICY,
        }
        if draft["operations"]:
            last = draft["operations"][-1]
            result["last_operation"] = {
                key: last[key]
                for key in ("action", "state", "attachment_id", "error_code")
                if key in last
            }
        if draft["state"] == "uncertain":
            result["message"] = UNCERTAIN
        if draft.get("review_only"):
            result.update(
                review_only=True,
                review_id=draft["review_id"],
                message="Ce dossier a été transféré vers un réexamen. "
                "Utiliser les outils de réexamen ; aucun nouveau dépôt de soins n'est permis.",
            )
        return result

    def mutable(self, draft: dict, expected_revision: int | None = None) -> None:
        if draft.get("review_only"):
            raise CfeError(
                "Ce dossier relève d'un réexamen : utiliser les outils cfe_*_review. "
                "Aucun téléversement ni nouveau dépôt de soins n'est permis."
            )
        if draft["state"] == "uncertain":
            raise CfeError(UNCERTAIN)
        if draft["state"] == "submitted":
            raise CfeError("Ce dossier a déjà été accepté par l'API CFE.")
        if expected_revision is not None and draft["revision"] != expected_revision:
            raise CfeError("Le dossier a changé ; relire son récapitulatif et sa révision.")

    async def schema(self, code: str) -> dict:
        return care_schema(code, await self.client.catalog())

    async def prepare(self, claim: dict) -> dict:
        async with self.guard():
            account = await self.client.account_context()
            normalized, payload, limits = normalize_claim(
                claim, await self.client.catalog(), account
            )
            draft_id = uuid4().hex
            private_folder(self.directory(draft_id))
            draft = {
                "version": 1,
                "id": draft_id,
                "revision": 1,
                "state": "draft",
                "subject": account["subject"],
                "owner": account["owner"],
                "created_at": time.time(),
                "input": normalized,
                "payload": payload,
                "limits": limits,
                "attachments": [],
                "ref_id": "",
                "operations": [],
            }
            self.save(draft)
            return self.view(draft)

    async def get(self, draft_id: str) -> dict:
        async with self.guard():
            return self.view(self.load(draft_id, await self.client.account_context()))

    async def list(self) -> dict:
        async with self.guard():
            account = await self.client.account_context()
            records = []
            for path in sorted(self.folder.iterdir()):
                if not re.fullmatch(r"[0-9a-f]{32}", path.name):
                    continue
                draft = read_private_json(self.directory(path.name) / "draft.json")
                if (
                    draft.get("subject") != account["subject"]
                    or draft.get("owner") != account["owner"]
                ):
                    continue
                item = self.view(self.load(path.name, account))
                records.append(
                    {
                        k: item[k]
                        for k in (
                            "draft_id", "revision", "state", "updated_at", "receipt",
                            "review_only", "review_id",
                        )
                        if k in item
                    }
                )
            return {
                "source": "local_mcp",
                "scope": "Dossiers créés avec ce MCP uniquement",
                "records": records,
            }

    async def update(self, draft_id: str, expected_revision: int, claim: dict) -> dict:
        async with self.guard():
            account = await self.client.account_context()
            draft = self.load(draft_id, account)
            self.mutable(draft, expected_revision)
            if any(row["state"] != "local" for row in draft["attachments"]):
                raise CfeError("Retirer les pièces déjà téléversées avant de modifier les soins.")
            clean, payload, limits = normalize_claim(claim, await self.client.catalog(), account)
            if len(draft["attachments"]) > limits["attachments"]:
                raise CfeError("Trop de justificatifs pour le nouveau pays ; retirer des pièces.")
            draft.update(
                input=clean, payload=payload, limits=limits, server_validation=None, state="draft"
            )
            draft["revision"] += 1
            self.save(draft)
            return self.view(draft)

    async def attach(self, draft_id: str, path: str, document_type: str) -> dict:
        if document_type not in DOCUMENT_TYPES:
            raise CfeError("Type de pièce attendu : feuille, ordonnance, facture ou autre.")
        async with self.guard():
            draft = self.load(draft_id, await self.client.account_context())
            self.mutable(draft)
            if len(draft["attachments"]) >= draft["limits"]["attachments"]:
                raise CfeError("Nombre maximal de justificatifs atteint pour ce dossier.")
            attachment = await asyncio.to_thread(
                snapshot_attachment, path, self.directory(draft_id), uuid4().hex
            )
            if attachment["sha256"] in {row["sha256"] for row in draft["attachments"]}:
                (self.directory(draft_id) / attachment["blob"]).unlink()
                raise CfeError("Ce justificatif est déjà présent dans le dossier.")
            attachment["type"] = document_type
            draft["attachments"].append(attachment)
            draft["revision"] += 1
            draft["state"] = "draft"
            self.save(draft)
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
            hashlib.sha256(content).hexdigest() != attachment["sha256"]
            or len(content) != attachment["bytes"]
        ):
            raise CfeError(
                "Le justificatif a changé depuis sa préparation ; opération interrompue."
            )
        return content

    def delivery(self, draft: dict, attachment: dict) -> tuple[bytes, dict]:
        content = self.content(draft, attachment)
        return delivery_document(content, attachment, self.directory(draft["id"]))

    async def preflight(self, draft: dict, account: dict) -> list[dict]:
        clean, payload, limits = normalize_claim(
            draft["input"], await self.client.catalog(), account
        )
        if payload != draft["payload"] or clean != draft["input"]:
            raise CfeError("Le formulaire CFE a changé ; mettre le brouillon à jour.")
        if not 1 <= len(draft["attachments"]) <= limits["attachments"]:
            raise CfeError(
                f"Ajouter entre 1 et {limits['attachments']} justificatifs avant l'envoi."
            )
        documents = []
        for attachment in draft["attachments"]:
            _, document = await asyncio.to_thread(self.delivery, draft, attachment)
            if attachment["state"] == "uploaded" and attachment.get("sent_document") != document:
                raise CfeError(
                    "Une pièce déjà téléversée ne respecte pas la politique de métadonnées. "
                    "La retirer du dossier distant puis joindre sa copie nettoyée avant dépôt."
                )
            documents.append(document)
        draft["limits"] = limits
        return documents

    async def check_server(self, draft: dict) -> None:
        for soin in draft["payload"]["soins"]:
            await self.client.write_action(
                "checkCare",
                {"body": canonical({"incident": draft["payload"]["incident"], "soin": soin})},
                subject=draft["subject"],
                owner=draft["owner"],
            )
        draft["server_validation"] = {
            "payload_sha256": digest(draft["payload"]),
            "validated_at": time.time(),
            "care_count": len(draft["payload"]["soins"]),
        }
        self.save(draft)

    async def validate(self, draft_id: str, server_check: bool = False) -> dict:
        async with self.guard():
            account = await self.client.account_context()
            draft = self.load(draft_id, account)
            self.mutable(draft)
            documents = await self.preflight(draft, account)
            if server_check:
                draft["server_validation"] = None
                self.save(draft)
                await self.check_server(draft)
            return {
                **self.view(draft), "locally_valid": True, "checked_on_server": server_check,
                "outbound_documents": documents,
            }

    async def journaled_write(
        self, draft: dict, action: str, body: dict, attachment: dict | None = None
    ) -> dict:
        operation = {
            "id": uuid4().hex,
            "action": action,
            "state": "inflight",
            "started_at": time.time(),
        }
        if attachment:
            operation["attachment_id"] = attachment["id"]
        draft["operations"].append(operation)
        draft["state"] = {
            "addDocument": "uploading",
            "deleteDocument": "deleting",
            "sendRequest": "submitting",
        }[action]
        self.save(draft)  # Durable intent BEFORE any network write, including token renewal.
        try:
            result = await self.client.write_action(
                action, body, subject=draft["subject"], owner=draft["owner"]
            )
        except Exception as error:
            rejected = isinstance(error, MobileApiError) and error.rejected
            operation["state"] = "rejected" if rejected else "uncertain"
            operation["finished_at"] = time.time()
            if isinstance(error, MobileApiError):
                operation["error_code"] = error.code
            draft["state"] = "draft" if rejected and action != "sendRequest" else "uncertain"
            self.save(draft)
            if draft["state"] == "uncertain":
                raise CfeError(UNCERTAIN) from None
            raise
        # Persist a private acknowledgement, without printing server contents or any token.
        operation["state"] = "accepted"
        operation["finished_at"] = time.time()
        operation["acknowledgement"] = result
        self.save(draft)
        return result

    async def upload(self, draft_id: str) -> dict:
        async with self.guard():
            account = await self.client.account_context()
            draft = self.load(draft_id, account)
            self.mutable(draft)
            await self.preflight(draft, account)
            # Check the current payload before transmitting any supporting documents.
            await self.check_server(draft)
            for attachment in draft["attachments"]:
                if attachment["state"] == "uploaded":
                    continue
                if attachment["state"] != "local":
                    raise CfeError(UNCERTAIN)
                content, document = await asyncio.to_thread(self.delivery, draft, attachment)
                attachment["sent_document"] = document
                acknowledgement = await self.journaled_write(
                    draft,
                    "addDocument",
                    {
                        "type": DOCUMENT_TYPES[attachment["type"]],
                        "file_name": document["name"],
                        "file_content": base64.b64encode(content).decode("ascii"),
                        "ref_id": draft["ref_id"],
                    },
                    attachment,
                )
                remote = acknowledgement["response"]
                if draft["ref_id"] and draft["ref_id"] != remote["ref_id"]:
                    draft["state"] = "uncertain"
                    self.save(draft)
                    raise CfeError(
                        "La CFE a changé la référence des pièces ; vérifier avant de poursuivre."
                    )
                if remote["file_id"] in {
                    row.get("remote", {}).get("file_id") for row in draft["attachments"]
                }:
                    draft["state"] = "uncertain"
                    self.save(draft)
                    raise CfeError("La CFE a renvoyé un identifiant de pièce déjà utilisé.")
                draft["ref_id"] = remote["ref_id"]
                attachment.update(
                    state="uploaded", remote={k: remote[k] for k in ("ref_id", "file_id")}
                )
                draft["revision"] += 1
                draft["state"] = "draft"
                self.save(draft)
            draft["state"] = "ready"
            self.save(draft)
            return self.view(draft)

    async def remove(self, draft_id: str, attachment_id: str) -> dict:
        async with self.guard():
            draft = self.load(draft_id, await self.client.account_context())
            self.mutable(draft)
            attachment = next(
                (row for row in draft["attachments"] if row["id"] == identifier(attachment_id)),
                None,
            )
            if attachment is None:
                raise CfeError("Cette pièce n'appartient pas à ce dossier local.")
            if attachment["state"] == "uploaded":
                await self.journaled_write(
                    draft,
                    "deleteDocument",
                    {"file_id": attachment["remote"]["file_id"]},
                    attachment,
                )
            elif attachment["state"] != "local":
                raise CfeError(UNCERTAIN)
            draft["attachments"].remove(attachment)
            if not any(row["state"] == "uploaded" for row in draft["attachments"]):
                draft["ref_id"] = ""
            draft["revision"] += 1
            draft["state"] = "draft"
            self.save(draft)
            # Preserve the snapshot for audit/recovery; it is no longer part of the claim.
            return self.view(draft)

    def submission_key(self, draft: dict) -> str:
        incident = {k: v for k, v in draft["payload"]["incident"].items() if k != "new_commentaire"}
        soins = []
        for soin in draft["payload"]["soins"]:
            soins.append(
                {
                    **soin,
                    "new_montantprisenchargeparorganismelocal": soin.get(
                        "new_montantprisenchargeparorganismelocal"
                    )
                    or "0",
                }
            )
        # Ignore filenames, comments and attachment additions: they must not enable double billing.
        return digest(
            {
                "subject": draft["subject"],
                "incident": incident,
                "soins": sorted(soins, key=canonical),
            }
        )

    async def submit(self, draft_id: str, expected_revision: int) -> dict:
        async with self.guard():
            account = await self.client.account_context()
            draft = self.load(draft_id, account)
            if draft["state"] == "submitted":
                return {**self.view(draft), "already_submitted": True}
            self.mutable(draft, expected_revision)
            await self.preflight(draft, account)
            if not draft["ref_id"] or any(
                row["state"] != "uploaded" for row in draft["attachments"]
            ):
                raise CfeError("Téléverser toutes les pièces avant de déposer le dossier.")
            ledger_path = self.folder / "submissions.json"
            ledger = read_private_json(ledger_path) if ledger_path.exists() else {}
            key = self.submission_key(draft)
            if key in ledger:
                raise CfeError(
                    "Ces soins ont déjà fait l'objet d'une tentative de dépôt avec ce MCP. "
                    "Vérifier le dossier " + ledger[key]["draft_id"] + " ; aucun nouvel envoi."
                )
            await self.check_server(draft)
            ledger[key] = {"draft_id": draft_id, "state": "inflight", "started_at": time.time()}
            write_private_json(ledger_path, ledger)
            acknowledgement = await self.journaled_write(
                draft,
                "sendRequest",
                {"body": canonical(draft["payload"]), "careId": draft["ref_id"]},
            )
            receipt_path = self.directory(draft_id) / "receipt.json"
            write_private_json(receipt_path, acknowledgement)
            draft["receipt"] = {
                "status": "accepted_by_api",
                "accepted_at": time.time(),
                "path": str(receipt_path),
                "message": "Dépôt accepté par l'API ; remboursement soumis au traitement CFE.",
            }
            draft["state"] = "submitted"
            draft["revision"] += 1
            self.save(draft)
            ledger[key].update(state="submitted", accepted_at=draft["receipt"]["accepted_at"])
            write_private_json(ledger_path, ledger)
            return self.view(draft)
