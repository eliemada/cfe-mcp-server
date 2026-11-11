import asyncio
import io

import pikepdf
import pytest
from pdf_samples import sample_pdf
from test_reviews import ACCOUNT, REVIEW, LocalClient

from cfe_mcp.client import CfeError
from cfe_mcp.reviews import ReviewManager
from cfe_mcp.storage import read_private_json, write_private_json

OWNER = "11111111-1111-4111-8111-111111111111"
ORIGINAL = "22222222-2222-4222-8222-222222222222"
REMOTE = "33333333-3333-4333-8333-333333333333"


class Portal:
    def __init__(self):
        self.sends = 0
        self.verifications = 0
        self.fail = None
        self.documents = []

    async def bind(self, draft):
        if self.fail == "bind":
            raise CfeError("Compte du portail différent")
        return {"portal_owner": OWNER, "original_request_id": ORIGINAL}

    async def send(self, draft, documents, checkpoint):
        if self.fail == "before_create":
            raise RuntimeError("sensitive browser internals")
        self.sends += 1
        for content, document in documents:
            with pikepdf.Pdf.open(io.BytesIO(content)) as pdf:
                assert "/Info" not in pdf.trailer
            assert document["name"].startswith("piece-")
        self.documents = documents
        checkpoint("creating")
        if self.fail == "creating":
            raise TimeoutError("sensitive upstream details")
        checkpoint("created", remote_id=REMOTE)
        await asyncio.sleep(0.01)
        if self.fail == "created":
            raise TimeoutError("sensitive upstream details")
        for _, document in documents:
            checkpoint("uploading", document=document)
            checkpoint("attached", document=document)
        if self.fail == "attached":
            raise TimeoutError("sensitive upstream details")
        checkpoint("submitting")
        return {"request_id": REMOTE, "message": "Received"}

    async def verify(self, draft, folder):
        self.verifications += 1
        if self.fail == "verify":
            raise CfeError("Récapitulatif indisponible")
        return {
            "request_id": draft["remote_id"], "status": "Envoyée à la CFE",
            "server_documents_verified": True, "server_message_verified": True,
        }


@pytest.fixture
def setup(tmp_path):
    client, portal = LocalClient(tmp_path / "data"), Portal()
    proof = tmp_path / "proof.pdf"
    proof.write_bytes(sample_pdf())
    return ReviewManager(client, portal=portal), portal, proof


async def prepare(setup, values=None):
    manager, _, proof = setup
    draft = await manager.prepare(values or REVIEW)
    return await manager.attach(draft["draft_id"], str(proof), "facture")


async def test_submit_sanitizes_checks_receipt_and_is_idempotent(setup):
    manager, portal, proof = setup
    draft = await prepare(setup)
    proof.write_bytes(b"changed original")
    result = await manager.submit(draft["draft_id"], draft["revision"])
    assert result["state"] == "submitted"
    assert result["delivery"]["receipt"]["server_documents_verified"]
    assert result["delivery"]["receipt"]["server_message_verified"]
    assert portal.sends == portal.verifications == 1
    assert await manager.submit(draft["draft_id"], draft["revision"]) == result
    assert portal.sends == portal.verifications == 1
    assert result["outbound_documents"][0]["sha256"] != draft["attachments"][0]["sha256"]


async def test_duplicate_with_changed_text_is_blocked(setup):
    manager, portal, _ = setup
    draft = await prepare(setup)
    await manager.submit(draft["draft_id"], draft["revision"])
    other = await prepare(setup, {**REVIEW, "message": "Different wording, same request."})
    with pytest.raises(CfeError, match="doublon"):
        await manager.submit(other["draft_id"], other["revision"])
    assert portal.sends == 1


async def test_older_supervised_submission_is_also_a_duplicate(setup):
    manager, portal, _ = setup
    draft = await prepare(setup)
    stored = manager.load(draft["draft_id"], ACCOUNT)
    stored.update(state="submitted", remote_id=REMOTE)
    manager.save(stored)
    assert not (manager.folder / "submissions.json").exists()
    other = await prepare(setup)
    with pytest.raises(CfeError, match="doublon"):
        await manager.submit(other["draft_id"], other["revision"])
    assert portal.sends == 0


async def test_concurrent_calls_send_once(setup):
    manager, portal, _ = setup
    draft = await prepare(setup)
    values = await asyncio.gather(*(
        manager.submit(draft["draft_id"], draft["revision"]) for _ in range(2)
    ))
    assert all(v["state"] == "submitted" for v in values)
    assert portal.sends == 1


@pytest.mark.parametrize("stage", ["creating", "created", "attached", "verify"])
async def test_interruption_blocks_retry_and_clones_and_refresh_never_sends(setup, stage):
    manager, portal, _ = setup
    draft = await prepare(setup)
    portal.fail = stage
    with pytest.raises(CfeError, match="incertain") as error:
        await manager.submit(draft["draft_id"], draft["revision"])
    assert "sensitive" not in str(error.value)
    restarted = ReviewManager(manager.client, portal=portal)
    assert (await restarted.get(draft["draft_id"]))["state"] == "uncertain"
    with pytest.raises(CfeError, match="incertain"):
        await restarted.submit(draft["draft_id"], draft["revision"])
    other = await prepare(setup)
    with pytest.raises(CfeError, match="doublon"):
        await restarted.submit(other["draft_id"], other["revision"])
    portal.fail = None
    result = await restarted.get(draft["draft_id"], refresh=True)
    assert result["state"] == ("uncertain" if stage == "creating" else "submitted")
    assert portal.sends == 1


@pytest.mark.parametrize("stage", ["bind", "before_create"])
async def test_failure_before_any_remote_transition_is_retryable(setup, stage):
    manager, portal, _ = setup
    draft = await prepare(setup)
    portal.fail = stage
    with pytest.raises(CfeError):
        await manager.submit(draft["draft_id"], draft["revision"])
    assert (await manager.get(draft["draft_id"]))["state"] == "draft"
    assert portal.sends == 0
    assert manager.ledger() == {}
    portal.fail = None
    assert (await manager.submit(draft["draft_id"], draft["revision"]))["state"] == "submitted"


async def test_crash_after_global_intent_recovers_id_and_never_recreates(setup):
    manager, portal, _ = setup
    draft = await prepare(setup)
    stored = manager.load(draft["draft_id"], ACCOUNT)
    write_private_json(manager.folder / "submissions.json", {
        manager.submission_key(stored): {"draft_id": draft["draft_id"], "remote_id": REMOTE},
    })
    result = await manager.get(draft["draft_id"])
    assert result["state"] == "uncertain"
    assert result["delivery"]["remote_id"] == REMOTE
    with pytest.raises(CfeError, match="incertain"):
        await manager.submit(draft["draft_id"], draft["revision"])
    assert portal.sends == 0


async def test_wrong_revision_or_modified_snapshot_never_sends(setup):
    manager, portal, _ = setup
    draft = await prepare(setup)
    with pytest.raises(CfeError, match="révision"):
        await manager.submit(draft["draft_id"], draft["revision"] + 1)
    stored = manager.load(draft["draft_id"], ACCOUNT)
    path = manager.directory(draft["draft_id"]) / stored["attachments"][0]["blob"]
    path.write_bytes(b"changed")
    with pytest.raises(CfeError):
        await manager.submit(draft["draft_id"], draft["revision"])
    assert portal.sends == 0 and manager.ledger() == {}


async def test_invalid_metadata_copy_never_falls_back_to_original(setup, monkeypatch):
    manager, portal, _ = setup
    draft = await prepare(setup)

    def refuse(*args):
        raise CfeError("Nettoyage impossible")

    monkeypatch.setattr("cfe_mcp.reviews.delivery_document", refuse)
    with pytest.raises(CfeError, match="Nettoyage"):
        await manager.submit(draft["draft_id"], draft["revision"])
    assert portal.sends == 0 and manager.ledger() == {}


async def test_wrong_mobile_identity_cannot_send_or_refresh(setup):
    manager, portal, _ = setup
    draft = await prepare(setup)
    await manager.submit(draft["draft_id"], draft["revision"])
    manager.client.account["subject"] = "different-person"
    for call in (
        manager.submit(draft["draft_id"], draft["revision"]),
        manager.get(draft["draft_id"], refresh=True),
    ):
        with pytest.raises(CfeError, match="autre connexion"):
            await call
    assert portal.sends == portal.verifications == 1


async def test_message_limit_fails_locally_before_any_portal_action(setup):
    manager, portal, _ = setup
    draft = await prepare(setup, {**REVIEW, "message": "x" * 2500})
    for call in (
        manager.validate(draft["draft_id"]),
        manager.submit(draft["draft_id"], draft["revision"]),
    ):
        with pytest.raises(CfeError, match="2 500"):
            await call
    assert portal.sends == 0 and manager.ledger() == {}
    stored = read_private_json(manager.directory(draft["draft_id"]) / "review.json")
    assert stored["state"] == "draft"
