import base64
import hashlib
import json
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pdf_samples import sample_pdf
from playwright.async_api import async_playwright

from cfe_mcp.client import COMPOSE_PATH, CfeError
from cfe_mcp.documents import POLICY
from cfe_mcp.review_delivery import (
    CONFIRMATION,
    FORM,
    THEME,
    ReviewBrowser,
    ReviewPortal,
    pdf_receipt_matches,
    review_message,
)

OWNER = "11111111-1111-4111-8111-111111111111"
SUBJECT = "22222222-2222-4222-8222-222222222222"
REMOTE = "33333333-3333-4333-8333-333333333333"
ORIGINAL = "44444444-4444-4444-8444-444444444444"
REFERENCE = "WEB-CAS-0000000-TEST00"
AUTH = (
    "<script>var isPortalUserLoggedIn = true;"
    f"var user = {{contactId:'{OWNER}',userName:'{SUBJECT}'}};</script>"
)


def example_draft():
    return {
        "subject_id": SUBJECT, "portal_owner": OWNER, "remote_id": REMOTE,
        "input": {"subject": "Synthetic test", "message": "No real medical information.",
                  "request_number": "TEST-000"},
        "attachments": [{"id": "offline"}],
    }


def document(content, name="piece-offline.pdf"):
    return {"policy": POLICY, "name": name, "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest()}


@pytest.mark.parametrize("scenario", [
    "success", "wrong_account", "wrong_form", "wrong_attachment", "changed_message",
    "no_ack", "local_success_only",
])
async def test_browser_form_all_network_intercepted(scenario):
    """Real DOM and multipart stages, but every request terminates inside this test."""
    draft = example_draft()
    documents = [(f"%PDF-1.4\nOFFLINE {i}".encode(), None) for i in range(3)]
    documents = [(data, document(data, f"piece-{i}.pdf"))
                 for i, (data, _) in enumerate(documents)]
    events, stages = [], []
    auth = AUTH.replace(SUBJECT, ORIGINAL) if scenario == "wrong_account" else AUTH
    form = "unexpected" if scenario == "wrong_form" else FORM
    attached_to = ORIGINAL if scenario == "wrong_attachment" else REMOTE
    frame_url = f"/_portal/modal-form-template-path/test?refentity=incident&refid={attached_to}"
    first = auth + f"""
    <input id="EntityFormView_EntityID" value="">
    <select id="new_theme"><option value="{THEME}">Remboursement</option></select>
    <input id="new_formulaireportaildelincident" value="{form}">
    <button id="NextButton" onclick="location.href='{COMPOSE_PATH}?step=2'">Suivant</button>
    <script>function Page_ClientValidate() {{ return true; }}</script>
    """
    final_query = (
        "step=done" if scenario == "local_success_only" else "success=true&type=messagerie"
    )
    second = AUTH + f"""
    <input id="EntityFormView_EntityID" value="{REMOTE}">
    <input id="new_theme" value="{THEME}">
    <textarea id="description" maxlength="2500"></textarea>
    <div id="Subgrid_PJ"><a class="create-action" href="#" onclick="openFile();return false;">
      Ajouter</a><table><tbody></tbody></table><div id="modal"></div></div>
    <button id="NextButton" style="display:none">Hidden submit</button>
    <button class="send-btn" onclick="sendMessage()">Envoyer</button>
    <script>
    let fileIndex=0;
    function openFile() {{
        document.querySelector('#modal').innerHTML='';
        const frame=document.createElement('iframe');
        frame.src='{frame_url}&index='+(++fileIndex);
        document.querySelector('#modal').append(frame);
    }}
    window.addEventListener('message', e => {{
        if(e.origin !== location.origin || !e.data.name) return;
        const row=document.createElement('tr'); row.dataset.id='row-'+fileIndex;
        const cell=document.createElement('td'); cell.textContent=e.data.name;
        row.append(cell); document.querySelector('#Subgrid_PJ tbody').append(row);
    }});
    function Page_ClientValidate() {{
        if ({json.dumps(scenario)} === 'changed_message')
            document.querySelector('#description').value='Unapproved replacement';
        return true;
    }}
    function sendMessage() {{
        fetch('/test-submit', {{method:'POST',body:document.querySelector('#description').value}})
        .then(() => location.href='{COMPOSE_PATH}?{final_query}');
    }}
    </script>
    """
    modal = """
    <input type="file" id="AttachFile"><button id="InsertButton" disabled>Joindre</button>
    <script>
    const input=document.querySelector('#AttachFile');
    input.onchange=() => fetch('/test-upload', {method:'POST',body:input.files[0]}).then(() => {
        document.querySelector('#InsertButton').disabled=false;
    });
    document.querySelector('#InsertButton').onclick=() =>
        fetch('/test-attach', {method:'POST'}).then(() => {
            parent.postMessage({name:input.files[0].name},location.origin);
        });
    </script>
    """

    async def handler(route):
        request = route.request
        url = urlsplit(request.url)
        params = parse_qs(url.query)
        if url.path in {"/test-submit", "/test-upload", "/test-attach"}:
            events.append((url.path, request.post_data_buffer))
            await route.fulfill(body="ok")
        elif url.path == COMPOSE_PATH:
            if params.get("success") or params.get("step") == ["done"]:
                ack = "Processing" if scenario == "no_ack" else CONFIRMATION
                html = AUTH + f'<div id="success-message">{ack}</div>'
            elif params.get("step") == ["2"]:
                events.append(("created", None))
                html = second
            else:
                html = first
            await route.fulfill(content_type="text/html; charset=utf-8", body=html)
        elif url.path.startswith("/_portal/modal-form-template-path/"):
            await route.fulfill(content_type="text/html; charset=utf-8", body=modal)
        else:
            await route.fulfill(status=404, body="No live network fallback")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = await browser.new_context()
            await context.route("**/*", handler)
            context.set_default_timeout(3000)

            @asynccontextmanager
            async def test_context():
                yield context

            runner = ReviewBrowser(None)
            runner.context = test_context

            def checkpoint(stage, **values):
                stages.append(stage)
                if "remote_id" in values:
                    assert values["remote_id"] == REMOTE

            if scenario == "success":
                receipt = await runner.send(draft, documents, checkpoint)
                assert receipt["request_id"] == REMOTE
                assert receipt["document_count"] == 3
                assert stages == ["creating", "created"] + ["uploading", "attached"] * 3 + [
                    "submitting"
                ]
                assert [body for path, body in events if path == "/test-upload"] == [
                    data for data, _ in documents
                ]
                assert [body for path, body in events if path == "/test-submit"] == [
                    review_message(draft).encode()
                ]
            else:
                with pytest.raises(CfeError):
                    await runner.send(draft, documents, checkpoint)
                if scenario in {"wrong_account", "wrong_form"}:
                    assert events == stages == []
                elif scenario == "wrong_attachment":
                    assert events == [("created", None)]
                    assert stages == ["creating", "created"]
                elif scenario == "changed_message":
                    assert not any(path == "/test-submit" for path, _ in events)
                else:
                    assert sum(path == "/test-submit" for path, _ in events) == 1
        finally:
            await browser.close()


class ReadOnlyPortal:
    """Signed history and annotation fixture scoped to one account and one review."""

    def __init__(self, draft):
        self.data = {}
        self.links = []
        self.calls = []
        self.records = [
            {"id": ORIGINAL, "fields": {"new_numdemandegds": {"value": "TEST-000"}}},
            {"id": REMOTE, "fields": {
                "title": {"value": "Message à la CFE"},
                "new_statutportail": {"formatted": "Envoyée à la CFE"},
            }},
        ]
        self.add(OWNER, "piece-offline.pdf", b"%PDF-OFFLINE", "*WEB*")
        self.add(SUBJECT, REFERENCE + ".pdf",
                 sample_pdf(REFERENCE + " " + review_message(draft)), "")
        self.auth = AUTH

    def add(self, identifier, name, content, file_id):
        self.data[identifier] = {
            "filename": name, "documentbody": base64.b64encode(content).decode()
        }
        self.links.append(
            f'<a class="file-link" data-value="{identifier}" '
            f'data-file-id="{file_id}">{name}</a>'
        )

    async def page(self, path):
        assert path in {COMPOSE_PATH, f"/mes-demarches/detail/?id={REMOTE}"}
        return self.auth + "".join(self.links)

    async def csrf(self):
        return "offline-csrf"

    async def grid(self, collection, page, page_size, cookie):
        assert collection == "requests"
        assert (page, page_size, cookie) == (1, 50, "")
        return {"records": self.records, "has_more": False}

    async def request(self, method, path, *, headers):
        assert method == "GET"
        assert headers["__RequestVerificationToken"] == "offline-csrf"
        identifier = path.split("(")[1].split(")")[0]
        assert path == f"/_api/annotations({identifier})?$select=documentbody,filename,mimetype"
        self.calls.append(identifier)
        return httpx.Response(200, json=self.data[identifier])


@pytest.mark.parametrize("scenario", [
    "success", "wrong_owner", "wrong_status", "missing_document", "changed_document",
    "wrong_message", "ambiguous_summary", "no_summary",
])
async def test_readback_needs_account_status_exact_documents_and_full_message(tmp_path, scenario):
    draft = example_draft()
    draft["outbound_documents"] = [document(b"%PDF-OFFLINE")]
    client = ReadOnlyPortal(draft)
    portal = ReviewPortal(client)
    if scenario == "wrong_owner":
        client.auth = AUTH.replace(OWNER, ORIGINAL)
    elif scenario == "wrong_status":
        client.records[-1]["fields"]["new_statutportail"]["formatted"] = "Brouillon"
    elif scenario == "missing_document":
        client.links.pop(0)
    elif scenario == "changed_document":
        client.data[OWNER]["documentbody"] = base64.b64encode(b"changed").decode()
    elif scenario == "wrong_message":
        client.data[SUBJECT]["documentbody"] = base64.b64encode(
            sample_pdf(REFERENCE + " DIFFERENT MESSAGE")
        ).decode()
    elif scenario == "ambiguous_summary":
        client.links.append(client.links[-1])
    elif scenario == "no_summary":
        client.links.pop()
    if scenario == "success":
        binding = await portal.bind(draft)
        assert binding == {"portal_owner": OWNER, "original_request_id": ORIGINAL}
        receipt = await portal.verify(draft, tmp_path)
        assert receipt["reference"] == REFERENCE
        assert receipt["server_documents_verified"] and receipt["server_message_verified"]
        assert (tmp_path / "receipts" / f"{REFERENCE}.pdf").stat().st_mode & 0o777 == 0o600
        assert client.calls == [OWNER, SUBJECT]
    else:
        with pytest.raises(CfeError):
            await portal.verify(draft, tmp_path)
        assert not (tmp_path / "receipts").exists()


async def test_original_request_must_belong_to_account_and_be_unambiguous():
    draft = example_draft()
    client = ReadOnlyPortal(draft)
    client.records.pop(0)
    with pytest.raises(CfeError, match="absente ou ambiguë"):
        await ReviewPortal(client).bind(draft)
    client.records += [
        {"id": ORIGINAL, "fields": {"ticketnumber": {"value": "TEST-000"}}}
    ] * 2
    with pytest.raises(CfeError, match="absente ou ambiguë"):
        await ReviewPortal(client).bind(draft)


def test_pdf_receipt_requires_reference_and_entire_message():
    message = review_message(example_draft())
    content = sample_pdf(REFERENCE + " " + message)
    assert pdf_receipt_matches(content, REFERENCE, message)
    assert not pdf_receipt_matches(content, "WEB-CAS-DIFFERENT", message)
    assert not pdf_receipt_matches(content, REFERENCE, message + " Extra unconfirmed sentence.")
    assert not pdf_receipt_matches(b"%PDF-invalid", REFERENCE, message)
