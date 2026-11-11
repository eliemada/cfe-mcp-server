import asyncio
import copy
import json
import stat

import httpx
import pytest
from pdf_samples import sample_pdf
from test_mobile_api import RECIPIENT, envelope, transport_for
from test_mobile_api import mobile_root as mobile_root

from cfe_mcp.care import care_schema, normalize_claim
from cfe_mcp.claims import MAX_ATTACHMENT_BYTES, ClaimManager
from cfe_mcp.client import CfeError
from cfe_mcp.mobile_api import MobileApiClient, response_value
from cfe_mcp.storage import read_private_json, write_private_json

ACCOUNT = {
    "subject": "test-person",
    "owner": "42",
    "beneficiaries": [{"id": "42", "holder": True}, {"id": "43", "holder": False}],
}
CATALOG = {
    "countries": [
        {"countryCode": "100", "currencyCode": "EUR", "countryName": "FRANCE"},
        {"countryCode": "217", "currencyCode": "JPY", "countryName": "JAPON"},
    ],
    "circumstances": [{"value": "100000000"}, {"value": "100000001"}],
    "limits": {
        "NB_PJ_MAX_ETR": "10",
        "NB_PJ_MAX_FR": "5",
        "NB_SOINS_MAX_ETR": "5",
        "NB_SOINS_MAX_FR": "1",
    },
    "care_types": [
        {"code": code, "label": code, "path": [code]}
        for code in ["A_1_1_1", "A_1_7", "D_1_1", "L_1_1", "N_1"]
    ],
}
CLAIM = {
    "beneficiary_id": "42",
    "country_code": "217",
    "currency_code": "JPY",
    "circumstance_code": "100000000",
    "care_items": [{"code": "A_1_1_1", "dateCare": "2026-10-15", "amount": "12340"}],
}


def no_error(request):
    return httpx.Response(200, json={"error": "[]"})


def claim_transport(calls, **overrides):
    uploaded = []

    def upload(request):
        body = json.loads(request.content)
        assert body["ref_id"] == ("reference-1" if uploaded else "")
        uploaded.append(body)
        return envelope({"ref_id": "reference-1", "file_id": f"file-{len(uploaded)}"})

    actions = {
        "getRecipients": RECIPIENT,
        "getCurrencies": CATALOG["countries"],
        "getCircumstances": CATALOG["circumstances"],
        "getParams": CATALOG["limits"],
        "getActeDetails": CATALOG["care_types"],
        "checkCare": no_error,
        "addDocument": upload,
        "deleteDocument": no_error,
        "sendRequest": no_error,
    }
    actions.update(overrides)
    return transport_for(actions, calls)


def count(calls, action):
    return sum(request.url.path.endswith("_" + action) for request in calls)


@pytest.fixture
def invoice(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(sample_pdf())
    return path


async def prepared(manager, invoice):
    draft = await manager.prepare(copy.deepcopy(CLAIM))
    return await manager.attach(draft["draft_id"], str(invoice), "facture")


def test_exact_serializer_and_no_invented_event_date():
    clean, payload, limits = normalize_claim(CLAIM, CATALOG, ACCOUNT)
    assert clean["care_items"][0]["codeDevise"] == "JPY"
    assert payload["incident"] == {
        "new_peridt": "42",
        "benefIDT": "42",
        "new_typecontact": "adh",
        "new_codepays": "217",
        "new_codedevise": "JPY",
        "new_circonstance": "100000000",
        "new_datededemandederemboursement": None,
        "new_commentaire": "",
    }
    assert payload["soins"][0]["new_verification"] == "A_1_1_1"
    assert payload["soins"][0]["new_montant"] == "12340"
    assert payload["soins"][0]["new_absencedatedescription"] is False
    assert limits["care_items"] == 5
    dependent = {**CLAIM, "beneficiary_id": "43"}
    assert normalize_claim(dependent, CATALOG, ACCOUNT)[1]["incident"]["new_typecontact"] == "benef"


@pytest.mark.parametrize(
    "mutation",
    [
        {"beneficiary_id": "999"},
        {"country_code": "999"},
        {"currency_code": "NOT_A_CURRENCY"},
        {"circumstance_code": "100000001"},
        {"care_items": []},
        {"secret_extra": "no"},
        {"care_items": [{"code": "A_1_1_1", "dateCare": "2026-02-30", "amount": "10"}]},
        {"care_items": [{"code": "A_1_1_1", "dateCare": "2026-01-01", "amount": "NaN"}]},
        {"care_items": [{"code": "A_1_1_1", "dateCare": "2026-01-01", "amount": 1.2}]},
        {"care_items": [{"code": "A_1_1_1", "dateCare": "2026-01-01", "amount": "-1"}]},
        {
            "care_items": [
                {"code": "A_1_1_1", "dateCare": "2026-01-01", "amount": "1", "localAmount": "2"}
            ]
        },
        {
            "care_items": [
                {"code": "A_1_1_1", "dateCare": "2026-01-01", "amount": "1", "unknown": True}
            ]
        },
        {"care_items": [{"code": "B_1_1", "dateCare": "2026-01-01", "amount": "1"}]},
    ],
)
def test_bad_input_fails_before_any_network_write(mutation):
    with pytest.raises(CfeError):
        normalize_claim({**CLAIM, **mutation}, CATALOG, ACCOUNT)


def test_prescription_absence_alias_and_date_ranges():
    schema = care_schema("A_1_7", CATALOG)
    assert "isPrescribed" in schema["fields"] and "absDatePresc" not in schema["fields"]
    item = {"code": "A_1_7", "dateCare": "2026-01-01", "amount": "10,50", "isPrescribed": True}
    normalized, payload, _ = normalize_claim({**CLAIM, "care_items": [item]}, CATALOG, ACCOUNT)
    assert normalized["care_items"][0]["amount"] == "10.5"
    assert payload["soins"][0]["new_absencedatedescription"] is True
    with pytest.raises(CfeError, match="pas les deux"):
        normalize_claim(
            {**CLAIM, "care_items": [{**item, "datePrescribe": "2025-12-01"}]}, CATALOG, ACCOUNT
        )
    hospitalization = {
        "code": "N_1",
        "amount": "10",
        "sejourStart": "2026-01-10",
        "sejourEnd": "2026-01-01",
    }
    with pytest.raises(CfeError, match="date de fin"):
        normalize_claim({**CLAIM, "care_items": [hospitalization]}, CATALOG, ACCOUNT)


@pytest.mark.parametrize("error", ["[]", "{}", '{"errorType":"noError","errorText":""}'])
def test_write_ack_accepts_only_explicit_success_channel(error):
    assert response_value({"error": error}, "sendRequest")["accepted"] is True


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"responses": "{}"},
        {"error": "null"},
        {"error": '""'},
        {"error": '{"unknown":"value"}'},
        {"error": '[{"errorType":"denied"}]'},
        {"error": '[{"errorText":"Invalid care"}]'},
    ],
)
def test_missing_or_error_ack_never_becomes_success(body):
    with pytest.raises(CfeError):
        response_value(body, "sendRequest")


async def test_local_preparation_and_snapshot_never_send_claim_data(mobile_root, invoice):
    calls = []
    async with MobileApiClient(mobile_root, transport=claim_transport(calls)) as client:
        manager = ClaimManager(client)
        draft = await prepared(manager, invoice)
        result = await manager.validate(draft["draft_id"])
        snapshot = manager.directory(draft["draft_id"]) / (draft["attachments"][0]["id"] + ".pdf")
        mode = await asyncio.to_thread(lambda: stat.S_IMODE(snapshot.stat().st_mode))
        assert mode == 0o600 and result["locally_valid"]
        assert not result["checked_on_server"]
        assert [item["draft_id"] for item in (await manager.list())["records"]] == [
            draft["draft_id"]
        ]
    assert all(count(calls, action) == 0 for action in ("checkCare", "addDocument", "sendRequest"))


async def test_complete_mock_workflow_is_bound_and_does_not_resubmit(mobile_root, invoice):
    calls = []
    second = invoice.with_name("prescription.pdf")
    await asyncio.to_thread(second.write_bytes, sample_pdf("DIFFERENT OFFLINE TEST"))
    async with MobileApiClient(mobile_root, transport=claim_transport(calls)) as client:
        manager = ClaimManager(client)
        draft = await prepared(manager, invoice)
        draft = await manager.attach(draft["draft_id"], str(second), "ordonnance")
        uploaded = await manager.upload(draft["draft_id"])
        assert uploaded["state"] == "ready"
        await manager.upload(draft["draft_id"])
        assert count(calls, "addDocument") == 2
        with pytest.raises(CfeError, match="a changé"):
            await manager.submit(draft["draft_id"], draft["revision"])
        submitted = await manager.submit(draft["draft_id"], uploaded["revision"])
        assert submitted["state"] == "submitted"
        assert submitted["receipt"]["status"] == "accepted_by_api"
        again = await manager.submit(draft["draft_id"], uploaded["revision"])
        assert again["already_submitted"]
        assert count(calls, "sendRequest") == 1
        request = next(r for r in calls if r.url.path.endswith("_sendRequest"))
        body = json.loads(request.content)
        assert body["id"] == "42" and body["careId"] == "reference-1"
        assert set(json.loads(body["body"])) == {"incident", "soins"}
        for action in ("remove", "update"):
            with pytest.raises(CfeError, match="déjà été accepté"):
                if action == "remove":
                    await manager.remove(draft["draft_id"], draft["attachments"][0]["id"])
                else:
                    await manager.update(draft["draft_id"], submitted["revision"], CLAIM)
        assert count(calls, "deleteDocument") == 0


async def test_timeout_blocks_retry_after_restart(mobile_root, invoice):
    calls = []

    def timeout(request):
        raise httpx.ReadTimeout("private-account private-id", request=request)

    transport = claim_transport(calls, addDocument=timeout)
    async with MobileApiClient(mobile_root, transport=transport) as client:
        manager = ClaimManager(client)
        draft = await prepared(manager, invoice)
        with pytest.raises(CfeError, match="incertain"):
            await manager.upload(draft["draft_id"])
    async with MobileApiClient(mobile_root, transport=transport) as client:
        manager = ClaimManager(client)
        assert (await manager.get(draft["draft_id"]))["state"] == "uncertain"
        with pytest.raises(CfeError, match="incertain"):
            await manager.upload(draft["draft_id"])
    assert count(calls, "addDocument") == 1


async def test_crash_journal_blocks_retry_and_hides_other_account(mobile_root, invoice):
    calls = []
    async with MobileApiClient(mobile_root, transport=claim_transport(calls)) as client:
        manager = ClaimManager(client)
        draft = await prepared(manager, invoice)
        record = manager.load(draft["draft_id"], ACCOUNT)
        record["state"] = "submitting"
        manager.save(record)
        assert (await manager.get(draft["draft_id"]))["state"] == "uncertain"
        with pytest.raises(CfeError, match="autre connexion"):
            manager.load(draft["draft_id"], {**ACCOUNT, "subject": "other-person"})
        with pytest.raises(CfeError, match="invalide"):
            await manager.get("../../escape")
    assert count(calls, "sendRequest") == 0


@pytest.mark.parametrize("failure", ["tampered", "oversize", "format", "duplicate"])
async def test_bad_documents_fail_without_upload(mobile_root, invoice, failure):
    calls = []
    async with MobileApiClient(mobile_root, transport=claim_transport(calls)) as client:
        manager = ClaimManager(client)
        draft = await prepared(manager, invoice)
        if failure == "tampered":
            path = manager.directory(draft["draft_id"]) / (draft["attachments"][0]["id"] + ".pdf")
            await asyncio.to_thread(path.write_bytes, b"%PDF-1.4\nMODIFIED\n%%EOF")
            with pytest.raises(CfeError, match="a changé"):
                await manager.upload(draft["draft_id"])
        else:
            if failure == "oversize":
                await asyncio.to_thread(invoice.write_bytes, b"%PDF-" + b"a" * MAX_ATTACHMENT_BYTES)
            if failure == "format":
                await asyncio.to_thread(invoice.write_bytes, b"not-a-pdf")
            with pytest.raises(CfeError):
                await manager.attach(draft["draft_id"], str(invoice), "facture")
    assert count(calls, "addDocument") == count(calls, "checkCare") == 0


async def test_validation_failure_does_not_upload(mobile_root, invoice):
    calls = []

    def invalid(request):
        return httpx.Response(
            200, json={"error": '[{"errorType":"invalidCare","errorText":"bad"}]'}
        )

    async with MobileApiClient(
        mobile_root, transport=claim_transport(calls, checkCare=invalid)
    ) as client:
        manager = ClaimManager(client)
        draft = await prepared(manager, invoice)
        with pytest.raises(CfeError, match="invalidCare"):
            await manager.upload(draft["draft_id"])
    assert count(calls, "addDocument") == count(calls, "sendRequest") == 0


async def test_unknown_submit_and_changed_comment_cannot_bypass_ledger(mobile_root, invoice):
    calls = []

    def ambiguous(request):
        return httpx.Response(200, json={"responses": "{}"})

    def upload(request):
        return envelope({"ref_id": "reference-1", "file_id": "file-1"})

    transport = claim_transport(calls, sendRequest=ambiguous, addDocument=upload)
    async with MobileApiClient(mobile_root, transport=transport) as client:
        manager = ClaimManager(client)
        first = await prepared(manager, invoice)
        first = await manager.upload(first["draft_id"])
        with pytest.raises(CfeError, match="incertain"):
            await manager.submit(first["draft_id"], first["revision"])
        second = await manager.prepare({**CLAIM, "comment": "Different comment, same care"})
        second = await manager.attach(second["draft_id"], str(invoice), "facture")
        second = await manager.upload(second["draft_id"])
        with pytest.raises(CfeError, match="déjà fait l'objet"):
            await manager.submit(second["draft_id"], second["revision"])
    assert count(calls, "sendRequest") == 1


async def test_two_managers_cannot_double_submit(mobile_root, invoice):
    calls = []
    transport = claim_transport(calls)
    async with MobileApiClient(mobile_root, transport=transport) as first_client:
        async with MobileApiClient(mobile_root, transport=transport) as second_client:
            first, second = ClaimManager(first_client), ClaimManager(second_client)
            draft = await prepared(first, invoice)
            draft = await first.upload(draft["draft_id"])
            results = await asyncio.gather(
                first.submit(draft["draft_id"], draft["revision"]),
                second.submit(draft["draft_id"], draft["revision"]),
            )
            assert all(row["state"] == "submitted" for row in results)
    assert count(calls, "sendRequest") == 1


async def test_delete_is_limited_to_recorded_unsubmitted_files(mobile_root, invoice):
    calls = []
    async with MobileApiClient(mobile_root, transport=claim_transport(calls)) as client:
        manager = ClaimManager(client)
        draft = await prepared(manager, invoice)
        draft = await manager.upload(draft["draft_id"])
        with pytest.raises(CfeError, match="n'appartient pas"):
            await manager.remove(draft["draft_id"], "f" * 32)
        result = await manager.remove(draft["draft_id"], draft["attachments"][0]["id"])
        assert result["attachments"] == []
        assert count(calls, "deleteDocument") == 1
        request = next(r for r in calls if r.url.path.endswith("_deleteDocument"))
        assert json.loads(request.content)["file_id"] == "file-1"
        record = read_private_json(manager.directory(draft["draft_id"]) / "draft.json")
        assert record["ref_id"] == ""


async def test_write_checks_account_binding_and_never_retries(mobile_root):
    calls = []
    async with MobileApiClient(mobile_root, transport=claim_transport(calls)) as client:
        with pytest.raises(CfeError, match="a changé"):
            await client.write_action(
                "sendRequest", {"careId": "x", "body": "{}"}, subject="other", owner="42"
            )
    assert count(calls, "sendRequest") == 0


async def test_saved_ledger_prevents_resend_after_crash_before_http(mobile_root, invoice):
    calls = []
    async with MobileApiClient(mobile_root, transport=claim_transport(calls)) as client:
        manager = ClaimManager(client)
        draft = await prepared(manager, invoice)
        draft = await manager.upload(draft["draft_id"])
        private = manager.load(draft["draft_id"], ACCOUNT)
        write_private_json(
            manager.folder / "submissions.json",
            {manager.submission_key(private): {"draft_id": draft["draft_id"], "state": "inflight"}},
        )
        with pytest.raises(CfeError, match="incertain"):
            await manager.submit(draft["draft_id"], draft["revision"])
        assert (await manager.get(draft["draft_id"]))["state"] == "uncertain"
    assert count(calls, "sendRequest") == 0
