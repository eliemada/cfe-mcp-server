import asyncio
import copy
import hashlib
import io
import stat
from pathlib import Path

import pikepdf
import pytest
from test_claims import CLAIM, claim_transport, count
from test_claims import invoice as invoice
from test_mobile_api import mobile_root as mobile_root

from cfe_mcp.claims import ClaimManager
from cfe_mcp.client import CfeError
from cfe_mcp.documents import POLICY
from cfe_mcp.mobile_api import MobileApiClient
from cfe_mcp.reviews import ReviewManager, normalize_review
from cfe_mcp.storage import read_private_json, write_private_json

ACCOUNT = {
    "subject": "test-person", "owner": "42",
    "beneficiaries": [{"id": "42", "holder": True}],
}
REVIEW = {
    "beneficiary_id": "42",
    "request_number": "260000001",
    "statement_reference": "20260101-E",
    "subject": "Réexamen - dossier 260000001",
    "message": "Bonjour,\nMerci de revoir la ligne du dossier existant.\nAucun nouveau soin.",
    "care_location": "etr",
}


class LocalClient:
    def __init__(self, root, account=None):
        self.root = root
        self.account = copy.deepcopy(account or ACCOUNT)

    async def account_context(self):
        return copy.deepcopy(self.account)


@pytest.mark.parametrize("change", [
    {"beneficiary_id": "other"},
    {"request_number": "../../secret"},
    {"subject": "one\ntwo"},
    {"message": ""},
    {"message": "x\x00y"},
    {"care_location": "unknown"},
    {"payload": "unexpected"},
])
def test_review_rejects_wrong_identity_and_malformed_fields(change):
    with pytest.raises(CfeError):
        normalize_review({**REVIEW, **change}, ACCOUNT)


async def test_review_snapshot_export_and_revision(tmp_path, invoice):
    manager = ReviewManager(LocalClient(tmp_path / "data"))
    draft = await manager.prepare(REVIEW)
    with pytest.raises(CfeError, match="au moins une"):
        await manager.validate(draft["draft_id"])
    original = invoice.read_bytes()
    draft = await manager.attach(draft["draft_id"], str(invoice), "facture")
    invoice.write_bytes(b"%PDF-1.4\nSource modified after attachment")
    valid = await manager.validate(draft["draft_id"])
    assert valid["locally_valid"] and not valid["checked_on_server"]
    assert valid["delivery"]["submitted"] is False
    assert valid["delivery"]["remote_submission_supported"] is True
    exported = await manager.export(draft["draft_id"], draft["revision"])
    piece = Path(exported["exported_attachments"][0]["path"])
    message = Path(exported["message_path"])
    folder = Path(exported["export_directory"])
    output = await asyncio.to_thread(piece.read_bytes)
    assert output != original
    source_hash = hashlib.sha256(original).hexdigest()
    assert exported["exported_attachments"][0]["source_sha256"] == source_hash
    with pikepdf.Pdf.open(io.BytesIO(output)) as pdf:
        assert "/Info" not in pdf.trailer
        assert len(pdf.pages) == 1
    assert await asyncio.to_thread(message.read_text) == (
        f"Objet : {REVIEW['subject']}\n\n{REVIEW['message']}\n"
    )
    assert stat.S_IMODE((await asyncio.to_thread(message.stat)).st_mode) == 0o600
    assert stat.S_IMODE((await asyncio.to_thread(folder.stat)).st_mode) == 0o700
    assert await manager.export(draft["draft_id"], draft["revision"]) == exported
    changed = await manager.update(draft["draft_id"], draft["revision"], {
        **REVIEW, "message": "Message corrigé.",
    })
    with pytest.raises(CfeError, match="révision"):
        await manager.update(draft["draft_id"], draft["revision"], REVIEW)
    with pytest.raises(CfeError, match="révision"):
        await manager.export(draft["draft_id"], draft["revision"])
    later = await manager.export(draft["draft_id"], changed["revision"])
    assert later["export_directory"] != exported["export_directory"]


async def test_review_account_isolation(tmp_path, invoice):
    root = tmp_path / "data"
    owner = ReviewManager(LocalClient(root))
    draft = await owner.prepare(REVIEW)
    outsider = ReviewManager(LocalClient(root, {**ACCOUNT, "subject": "another-login"}))
    assert (await outsider.list())["records"] == []
    for action in (
        outsider.get(draft["draft_id"]),
        outsider.update(draft["draft_id"], 1, REVIEW),
        outsider.attach(draft["draft_id"], str(invoice), "facture"),
        outsider.export(draft["draft_id"], 1),
    ):
        with pytest.raises(CfeError, match="autre connexion"):
            await action


@pytest.mark.parametrize("state", ["sending", "uncertain", "submitted"])
async def test_transmitted_review_stays_readable_and_cannot_be_modified(tmp_path, invoice, state):
    manager = ReviewManager(LocalClient(tmp_path / "data"))
    draft = await manager.prepare(REVIEW)
    draft = await manager.attach(draft["draft_id"], str(invoice), "facture")
    stored = manager.load(draft["draft_id"], ACCOUNT)
    stored.update(
        state=state,
        remote_id="12345678-1234-1234-1234-123456789abc",
        last_stage=state,
        receipt={"status": "Envoyée à la CFE"} if state == "submitted" else None,
    )
    manager.save(stored)
    result = await manager.get(draft["draft_id"])
    stored = manager.load(draft["draft_id"], ACCOUNT)
    assert result["delivery"]["submitted"] is (state == "submitted")
    assert result["delivery"]["remote_id"] == stored["remote_id"]
    assert result["delivery"]["receipt"] == stored["receipt"]
    assert (await manager.list())["records"][0]["state"] == (
        "uncertain" if state == "sending" else state
    )
    for action in (
        manager.update(draft["draft_id"], draft["revision"], REVIEW),
        manager.attach(draft["draft_id"], str(invoice), "facture"),
        manager.remove(draft["draft_id"], draft["attachments"][0]["id"]),
    ):
        with pytest.raises(CfeError, match="transmission"):
            await action
    assert manager.load(draft["draft_id"], ACCOUNT) == stored


async def test_review_detects_snapshot_tampering_and_export_edits(tmp_path, invoice):
    manager = ReviewManager(LocalClient(tmp_path / "data"))
    draft = await manager.prepare(REVIEW)
    draft = await manager.attach(draft["draft_id"], str(invoice), "facture")
    with pytest.raises(CfeError, match="déjà présente"):
        await manager.attach(draft["draft_id"], str(invoice), "facture")
    exported = await manager.export(draft["draft_id"], draft["revision"])
    message = Path(exported["message_path"])
    await asyncio.to_thread(message.write_text, "Modification de l'utilisateur")
    with pytest.raises(CfeError, match="modifié"):
        await manager.export(draft["draft_id"], draft["revision"])
    assert await asyncio.to_thread(message.read_text) == "Modification de l'utilisateur"
    stored = manager.load(draft["draft_id"], ACCOUNT)
    blob = manager.directory(draft["draft_id"]) / stored["attachments"][0]["blob"]
    blob.write_bytes(b"%PDF-1.4\nChanged snapshot")
    with pytest.raises(CfeError, match="changé"):
        await manager.validate(draft["draft_id"])


async def test_review_rejects_symlink_and_traversal_exports(tmp_path, invoice):
    manager = ReviewManager(LocalClient(tmp_path / "data"))
    draft = await manager.prepare(REVIEW)
    draft = await manager.attach(draft["draft_id"], str(invoice), "facture")
    path = manager.directory(draft["draft_id"]) / "review.json"
    stored = read_private_json(path)
    stored["attachments"][0]["name"] = "../../escaped.pdf"
    write_private_json(path, stored)
    with pytest.raises(CfeError, match="Nom de pièce"):
        await manager.export(draft["draft_id"], draft["revision"])
    assert not (tmp_path / "escaped.pdf").exists()
    stored["attachments"][0]["name"] = "invoice.pdf"
    write_private_json(path, stored)
    folder = manager.directory(draft["draft_id"]) / f"export-{draft['revision']}-{POLICY}"
    (folder / "message.txt").unlink()
    target = tmp_path / "unrelated.txt"
    target.write_text("untouched")
    (folder / "message.txt").symlink_to(target)
    with pytest.raises(CfeError, match="symbolique"):
        await manager.export(draft["draft_id"], draft["revision"])
    assert target.read_text() == "untouched"


async def test_remove_review_attachment_keeps_private_snapshot(tmp_path, invoice):
    manager = ReviewManager(LocalClient(tmp_path / "data"))
    draft = await manager.prepare(REVIEW)
    draft = await manager.attach(draft["draft_id"], str(invoice), "facture")
    before = manager.load(draft["draft_id"], ACCOUNT)
    result = await manager.remove(draft["draft_id"], draft["attachments"][0]["id"])
    assert result["attachments"] == []
    assert (manager.directory(draft["draft_id"]) / before["attachments"][0]["blob"]).exists()


async def test_live_adapter_review_import_never_sends_and_blocks_original(mobile_root, invoice):
    calls = []
    async with MobileApiClient(mobile_root, transport=claim_transport(calls)) as client:
        claims = ClaimManager(client)
        reviews = ReviewManager(client)
        source = await claims.prepare(CLAIM)
        source = await claims.attach(source["draft_id"], str(invoice), "facture")
        values = {key: value for key, value in REVIEW.items() if key != "beneficiary_id"}
        draft = await reviews.import_claim(source["draft_id"], source["revision"], values)
        assert draft["attachments"][0]["name"] == invoice.name
        assert draft["attachments"][0]["sha256"] == source["attachments"][0]["sha256"]
        assert (await reviews.validate(draft["draft_id"]))["locally_valid"]
        assert await reviews.import_claim(source["draft_id"], source["revision"], values) == draft
        blocked = await claims.get(source["draft_id"])
        assert blocked["review_only"] and blocked["review_id"] == draft["draft_id"]
        assert (await claims.list())["records"][0]["review_only"]
        for action in (
            claims.upload(source["draft_id"]),
            claims.validate(source["draft_id"], True),
            claims.submit(source["draft_id"], blocked["revision"]),
        ):
            with pytest.raises(CfeError, match="réexamen"):
                await action
        for name in ("checkCare", "addDocument", "deleteDocument", "sendRequest"):
            assert count(calls, name) == 0


async def test_interrupted_import_stays_blocked_and_can_resume(mobile_root, invoice, monkeypatch):
    calls = []
    async with MobileApiClient(mobile_root, transport=claim_transport(calls)) as client:
        claims = ClaimManager(client)
        reviews = ReviewManager(client)
        source = await claims.prepare(CLAIM)
        source = await claims.attach(source["draft_id"], str(invoice), "facture")
        save = reviews.save
        def fail(_):
            raise OSError("Simulated disk interruption")
        monkeypatch.setattr(reviews, "save", fail)
        with pytest.raises(OSError):
            await reviews.import_claim(source["draft_id"], source["revision"], REVIEW)
        with pytest.raises(CfeError, match="réexamen"):
            await claims.upload(source["draft_id"])
        assert (await reviews.list())["records"] == []
        monkeypatch.setattr(reviews, "save", save)
        recovered = await reviews.import_claim(source["draft_id"], source["revision"], REVIEW)
        assert len(recovered["attachments"]) == 1
        assert len((await reviews.list())["records"]) == 1
        assert count(calls, "addDocument") == 0


async def test_import_rejects_stale_revision_and_remote_work(mobile_root, invoice):
    calls = []
    async with MobileApiClient(mobile_root, transport=claim_transport(calls)) as client:
        claims = ClaimManager(client)
        reviews = ReviewManager(client)
        source = await claims.prepare(CLAIM)
        source = await claims.attach(source["draft_id"], str(invoice), "facture")
        with pytest.raises(CfeError, match="révision|réexamen|dossier a changé",):
            await reviews.import_claim(source["draft_id"], 1, REVIEW)
        with pytest.raises(CfeError, match="Révision"):
            await reviews.import_claim(source["draft_id"], True, REVIEW)
        await claims.upload(source["draft_id"])
        current = await claims.get(source["draft_id"])
        with pytest.raises(CfeError, match="sans écriture"):
            await reviews.import_claim(source["draft_id"], current["revision"], REVIEW)
        assert not (await claims.get(source["draft_id"])).get("review_only")
