import asyncio
import base64
import json
import stat
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from cfe_mcp.client import CfeError
from cfe_mcp.mobile import MobileSession
from cfe_mcp.mobile_api import API_BASE, TECH_SCOPE, TECH_TOKEN_URL, MobileApiClient
from cfe_mcp.storage import read_private_json, write_private_json


@pytest.fixture
def mobile_root(tmp_path, monkeypatch):
    write_private_json(
        tmp_path / "mobile-service-config.json",
        {
            "client_id": "00000000-0000-0000-0000-000000000001",
            "client_secret": "synthetic-test-secret-never-real",
            "token_url": TECH_TOKEN_URL,
            "scope": TECH_SCOPE,
            "api_base": API_BASE,
        },
    )

    async def identity(self, **kwargs):
        return {"id_token": "private-id", "subject": "test-person", "expires_at": time.time() + 300}

    monkeypatch.setattr(MobileSession, "identity", identity)
    return tmp_path


def envelope(value, error=None):
    return httpx.Response(
        200, json={"responses": json.dumps(value), "error": json.dumps(error or {})}
    )


def transport_for(actions, calls):
    def handler(request):
        calls.append(request)
        if str(request.url) == TECH_TOKEN_URL:
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["client_credentials"]
            assert form["scope"] == [TECH_SCOPE]
            return httpx.Response(
                200, json={"access_token": "private-technical", "expires_in": 3600}
            )
        assert str(request.url).startswith(API_BASE + "new_api_applimob_")
        assert request.headers["authorization"] == "Bearer private-technical"
        action = str(request.url).removeprefix(API_BASE + "new_api_applimob_")
        body = json.loads(request.content)
        if action == "auth":
            assert body == {"id_token": "private-id"}
            return envelope({"peridt": "42", "access": True, "token": "private-account"})
        assert body["token"] == "private-account"
        if action in {"getRecipients", "getRemboursement", "getDocRemboursement"}:
            assert body["id"] == "42"
        value = actions[action]
        return value(request) if callable(value) else envelope(value)

    return httpx.MockTransport(handler)


RECIPIENT = {
    "perIdt": "42",
    "firstName": "Test",
    "lastName": "Person",
    "nir": "must-not-return",
    "depotPortailFDSL": "100000001",
    "contactsLies": [],
}
REFUNDS = {
    "docs": [
        {
            "id": "own-document",
            "benef": "Test Person",
            "montant": "42.00",
            "date": "2026-01-01",
            "ispayed": "true",
            "lien": "",
        }
    ]
}


async def test_reads_use_both_tokens_and_hide_nir(mobile_root):
    calls = []
    transport = transport_for({"getRecipients": RECIPIENT, "getRemboursement": REFUNDS}, calls)
    async with MobileApiClient(mobile_root, transport=transport) as client:
        status = await client.session_status()
        people = await client.beneficiaries()
        refunds = await client.reimbursements()
    assert status["authenticated"] and status["account_api_checked"]
    assert refunds["total"] == 1 and refunds["records"][0]["paid"] is True
    assert people["beneficiaries"][0]["id"] == "42"
    public = json.dumps([status, people, refunds])
    assert "private-" not in public and "must-not-return" not in public
    assert len([r for r in calls if r.url.path.endswith("_auth")]) == 1


async def test_foreign_document_is_never_requested(mobile_root):
    calls = []
    transport = transport_for({"getRemboursement": REFUNDS}, calls)
    async with MobileApiClient(mobile_root, transport=transport) as client:
        with pytest.raises(CfeError, match="compte connecté"):
            await client.download_statement("someone-elses-document")
    assert not any("getDocRemboursement" in str(r.url) for r in calls)


async def test_pdf_download_uses_private_generated_path(mobile_root):
    content = b"%PDF-1.4\nsynthetic-test\n%%EOF"
    calls = []
    transport = transport_for(
        {
            "getRemboursement": REFUNDS,
            "getDocRemboursement": {
                "filename": "../../escape.pdf",
                "filecontent": base64.b64encode(content).decode(),
            },
        },
        calls,
    )
    async with MobileApiClient(mobile_root, transport=transport) as client:
        result = await client.download_statement("own-document")
    path = Path(result["path"])
    assert path.parent == mobile_root / "downloads"
    contents, info = await asyncio.to_thread(lambda: (path.read_bytes(), path.stat()))
    assert contents == content
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert "filecontent" not in result


@pytest.mark.parametrize("invalid", ["wrong-host", "http", "redirect"])
async def test_technical_credentials_never_leave_configured_origin(mobile_root, invalid):
    calls = []
    if invalid != "redirect":
        path = mobile_root / "mobile-service-config.json"
        config = read_private_json(path)
        config["token_url"] = (
            "https://evil.example/token"
            if invalid == "wrong-host"
            else TECH_TOKEN_URL.replace("https:", "http:")
        )
        write_private_json(path, config)

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/token"})

    async with MobileApiClient(mobile_root, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CfeError):
            await client.session_status()
    assert len(calls) == (1 if invalid == "redirect" else 0)
    assert all(str(r.url) == TECH_TOKEN_URL for r in calls)


async def test_account_mismatch_fails(mobile_root):
    transport = transport_for({"getRecipients": {**RECIPIENT, "perIdt": "999"}}, [])
    async with MobileApiClient(mobile_root, transport=transport) as client:
        with pytest.raises(CfeError, match="adhérent connecté"):
            await client.beneficiaries()


async def test_server_error_is_not_reported_as_empty_success(mobile_root):
    def failed(request):
        return envelope({}, {"errorType": "denied", "errorText": "private-account private-id"})

    transport = transport_for({"getRecipients": failed}, [])
    async with MobileApiClient(mobile_root, transport=transport) as client:
        with pytest.raises(CfeError) as error:
            await client.session_status()
    assert "denied" in str(error.value) and "private" not in str(error.value)


async def test_no_generic_action_or_write_through_read_method(mobile_root):
    calls = []
    async with MobileApiClient(mobile_root, transport=transport_for({}, calls)) as client:
        for action in ["sendRequest", "deleteDocument", "../contacts", "https://evil.example"]:
            with pytest.raises(CfeError):
                await client._read(action)
    assert not calls
