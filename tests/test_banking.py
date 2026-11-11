import asyncio
import io
import json
from contextlib import asynccontextmanager

import pikepdf
import pytest
from pdf_samples import sample_pdf
from playwright.async_api import async_playwright

from cfe_mcp.banking import (
    ADD_PATH,
    BANK_PATH,
    BANK_TITLE,
    CONFIRMATION,
    BankBrowser,
    BankManager,
    bank_display,
    normalize_bank,
    partial_iban_length,
    portal_owner,
)
from cfe_mcp.client import AuthenticationRequired, CfeError
from cfe_mcp.storage import read_private_json, write_private_json

OWNER = "11111111-1111-4111-8111-111111111111"
COUNTRY = "22222222-2222-4222-8222-222222222222"
REMOTE = "33333333-3333-4333-8333-333333333333"
IBAN = "BE68539007547034"  # Published example, not a user's bank account.
AUTH = f"<script>var isPortalUserLoggedIn = true; var user = {{contactId:'{OWNER}'}};</script>"
OPTIONS = f'<select id="new_ibanpayscompte"><option value="{COUNTRY}">BELGIQUE</option></select>'


class Portal:
    def __init__(self):
        self.owner = OWNER
        self.records = []

    async def page(self, path):
        assert path in {BANK_PATH, ADD_PATH}
        auth = AUTH.replace(OWNER, self.owner)
        return auth + (OPTIONS if path == ADD_PATH else "IBAN remboursement : FR76xxxxx1234")

    async def grid(self, collection, page, page_size, cookie):
        assert collection == "requests"
        return {"records": self.records, "has_more": False}


class Browser:
    def __init__(self):
        self.calls = 0
        self.fail = False

    async def send(self, draft, content, checkpoint):
        assert content.startswith(b"%PDF-")
        assert draft["attachment"]["name"].startswith("piece-")
        with pikepdf.Pdf.open(io.BytesIO(content)) as pdf:
            assert "/Info" not in pdf.trailer
        self.calls += 1
        checkpoint("creating")
        checkpoint("created", remote_id=REMOTE)
        await asyncio.sleep(0.01)
        if self.fail:
            raise TimeoutError("sensitive upstream details")
        checkpoint("submitting")
        return {
            "request_id": REMOTE,
            "status": "Envoyée à la CFE",
            "activation": "pending_cfe_validation",
        }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("CFE_DATA_DIR", str(tmp_path / "private"))
    proof = tmp_path / "bank.pdf"
    proof.write_bytes(sample_pdf("OFFLINE BANK TEST"))
    portal, browser = Portal(), Browser()
    return BankManager(portal, browser), portal, browser, proof


async def prepare(setup):
    manager, _, _, proof = setup
    return await manager.prepare("Example Holder", IBAN, COUNTRY, str(proof), "TRWIBEB1XXX")


def test_valid_iban_and_correct_display_lengths():
    assert normalize_bank("Example Holder", "BE68 5390 0754 7034", COUNTRY)["iban"] == IBAN
    assert partial_iban_length(16) == 17  # Portal incorrectly requires 18.
    assert partial_iban_length(27) == 31  # French IBAN is unaffected.
    assert partial_iban_length(20) == 22
    assert normalize_bank("Example", "FR1420041010050500013M02606", COUNTRY)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"iban": "BE68539007547035"},
        {"iban": "BE68-539007547034"},
        {"holder": "Example <script>"},
        {"country_id": "bad"},
        {"bic": "INVALID!"},
    ],
)
def test_invalid_bank_data_is_rejected(kwargs):
    args = {"holder": "Example", "iban": IBAN, "country_id": COUNTRY}
    args.update(kwargs)
    with pytest.raises(CfeError):
        normalize_bank(**args)


def test_portal_identity_and_bank_parsing():
    assert portal_owner(AUTH) == OWNER
    with pytest.raises(AuthenticationRequired):
        portal_owner("Login")
    with pytest.raises(CfeError):
        portal_owner(AUTH + "contactId:'44444444-4444-4444-8444-444444444444'")
    assert bank_display("<p>IBAN remboursement : <b>FR76xxxx1234</b></p>") == {
        "reimbursement_iban": "FR76xxxx1234",
        "contribution_iban": None,
    }


async def test_prepare_is_local_and_freezes_proof(setup):
    manager, _, browser, proof = setup
    draft = await prepare(setup)
    assert draft["state"] == "draft"
    assert draft["changes_contributions"] is False
    assert draft["iban_masked"] == "BE68…7034"
    assert IBAN not in str(draft)
    assert browser.calls == 0
    stored = read_private_json(manager.directory(draft["draft_id"]) / "draft.json")
    proof.write_bytes(b"%PDF-1.4\nchanged original\n")
    result = await manager.submit(draft["draft_id"], 1)
    assert result["state"] == "submitted"
    assert result["receipt"]["activation"] == "pending_cfe_validation"
    assert stored["attachment"]["sha256"] == result["attachment"]["sha256"]


async def test_stale_revision_and_changed_snapshot_never_send(setup):
    manager, _, browser, _ = setup
    draft = await prepare(setup)
    with pytest.raises(CfeError, match="Révision"):
        await manager.submit(draft["draft_id"], 2)
    stored = read_private_json(manager.directory(draft["draft_id"]) / "draft.json")
    blob = manager.directory(draft["draft_id"]) / stored["attachment"]["blob"]
    blob.write_bytes(b"%PDF-1.4\ntampered\n")
    with pytest.raises(CfeError, match="changé"):
        await manager.submit(draft["draft_id"], 1)
    assert browser.calls == 0


async def test_account_isolation(setup):
    manager, portal, browser, _ = setup
    draft = await prepare(setup)
    portal.owner = "44444444-4444-4444-8444-444444444444"
    with pytest.raises(CfeError, match="autre compte"):
        await manager.submit(draft["draft_id"], 1)
    assert (await manager.list())["records"] == []
    assert browser.calls == 0


async def test_idempotent_submit_and_duplicate_draft(setup):
    manager, _, browser, _ = setup
    draft = await prepare(setup)
    first = await manager.submit(draft["draft_id"], 1)
    assert await manager.submit(draft["draft_id"], 1) == first
    another = await prepare(setup)
    with pytest.raises(CfeError, match="doublon"):
        await manager.submit(another["draft_id"], 1)
    assert browser.calls == 1


async def test_concurrent_submit_sends_once(setup):
    manager, _, browser, _ = setup
    draft = await prepare(setup)
    results = await asyncio.gather(
        manager.submit(draft["draft_id"], 1), manager.submit(draft["draft_id"], 1)
    )
    assert all(r["state"] == "submitted" for r in results)
    assert browser.calls == 1


async def test_timeout_blocks_same_and_new_draft_after_restart(setup):
    manager, portal, browser, _ = setup
    browser.fail = True
    draft = await prepare(setup)
    with pytest.raises(CfeError, match="incertain") as error:
        await manager.submit(draft["draft_id"], 1)
    assert "sensitive" not in str(error.value)
    restarted = BankManager(portal, browser)
    assert (await restarted.get(draft["draft_id"]))["state"] == "uncertain"
    with pytest.raises(CfeError, match="incertain"):
        await restarted.submit(draft["draft_id"], 1)
    another = await prepare(setup)
    with pytest.raises(CfeError, match="doublon"):
        await restarted.submit(another["draft_id"], 1)
    assert browser.calls == 1


async def test_crash_between_ledger_and_draft_is_uncertain(setup):
    manager, _, browser, _ = setup
    draft = await prepare(setup)
    stored = read_private_json(manager.directory(draft["draft_id"]) / "draft.json")
    write_private_json(
        manager.folder / "submissions.json",
        {
            manager.key(stored): {"draft_id": draft["draft_id"]},
        },
    )
    assert (await manager.get(draft["draft_id"]))["state"] == "uncertain"
    assert browser.calls == 0


def bank_request(status="Envoyée à la CFE"):
    return {
        "id": REMOTE,
        "fields": {
            "title": {"value": BANK_TITLE},
            "statecode": {"value": 0},
            "new_statutportail": {"formatted": status},
        },
    }


async def test_existing_pending_request_blocks_even_without_local_ledger(setup):
    manager, portal, browser, _ = setup
    draft = await prepare(setup)
    portal.records = [bank_request()]
    with pytest.raises(CfeError, match="déjà en cours"):
        await manager.submit(draft["draft_id"], 1)
    assert browser.calls == 0


async def test_refresh_reconciles_known_request_without_resending(setup):
    manager, portal, browser, _ = setup
    browser.fail = True
    draft = await prepare(setup)
    with pytest.raises(CfeError):
        await manager.submit(draft["draft_id"], 1)
    portal.records = [bank_request()]
    result = await manager.get(draft["draft_id"], refresh=True)
    assert result["state"] == "submitted"
    assert result["receipt"]["activation"] == "not_verified"
    assert browser.calls == 1


async def test_browser_start_failure_remains_retryable(setup):
    manager, _, browser, _ = setup
    draft = await prepare(setup)

    async def fail_before_checkpoint(*args):
        raise RuntimeError("browser missing")

    browser.send = fail_before_checkpoint
    with pytest.raises(CfeError, match="aucun envoi effectué"):
        await manager.submit(draft["draft_id"], 1)
    assert (await manager.get(draft["draft_id"]))["state"] == "draft"
    assert manager.ledger() == {}


async def test_details_includes_server_country_options(setup):
    manager, _, _, _ = setup
    result = await manager.details()
    assert result["countries"] == [{"id": COUNTRY, "label": "BELGIQUE"}]
    assert result["reimbursement_iban"] == "FR76xxxxx1234"


async def test_snapshot_permissions_and_maximum_size(setup):
    manager, _, _, proof = setup
    draft = await prepare(setup)
    path = manager.directory(draft["draft_id"]) / "draft.json"
    assert path.stat().st_mode & 0o777 == 0o600
    proof.write_bytes(b"%PDF-" + b"0" * 3_000_000)
    with pytest.raises(CfeError, match="3 Mo"):
        await manager.prepare("Example", IBAN, COUNTRY, str(proof))


async def test_attachment_path_traversal_rejected(setup):
    manager, _, browser, _ = setup
    draft = await prepare(setup)
    path = manager.directory(draft["draft_id"]) / "draft.json"
    stored = read_private_json(path)
    stored["attachment"]["blob"] = "../bank.pdf"
    write_private_json(path, stored)
    with pytest.raises(CfeError, match="Chemin"):
        await manager.submit(draft["draft_id"], 1)
    assert browser.calls == 0


@pytest.mark.parametrize(
    "scenario", ["success", "wrong_country", "wrong_attachment", "contributions", "no_ack"]
)
async def test_browser_form_with_all_network_intercepted(scenario):
    """Exercise real DOM, JS length bug, frame upload and navigation without reaching the CFE."""
    events = []
    remote = REMOTE if scenario != "wrong_attachment" else OWNER
    prefix = "BE" if scenario != "wrong_country" else "FR"
    first = (
        AUTH
        + f"""
    <input id="EntityFormView_EntityID" value="">
    <input id="new_titulaireducompte">
    <select id="new_ibanpayscompte"><option value="{COUNTRY}">BELGIQUE</option></select>
    <input type="button" id="NextButton" onclick="location.href='{ADD_PATH}?step=2'">
    """
    )
    frame_url = f"/_portal/modal-form-template-path/example?refentity=incident&refid={remote}"
    second = (
        AUTH
        + f"""
    <input id="EntityFormView_EntityID" value="{REMOTE}">
    <input id="partial_iban" maxlength="18">
    <input id="new_ibannumerocompte" maxlength="16" style="display:none">
    <input id="new_biccompte" style="display:none">
    <input type="radio" id="new_isdomprincipale_0" name="contributions" value="0">
    <input type="radio" id="new_isdomprincipale_1" name="contributions" value="1">
    <div id="AjouterRIB"><a class="create-action" href="#"
        onclick="document.querySelector('iframe').src='{frame_url}';return false;">Ajouter</a>
        <table><tbody></tbody></table><iframe></iframe></div>
    <input type="button" id="NextButton" value="Valider" onclick="submitBank()">
    <script>
    var countryCode={json.dumps(prefix)}, nbCaractere=16, isBIC=false;
    const partial=document.querySelector('#partial_iban');
    partial.oninput=() => {{
        partial.value=('XX'+partial.value.replace(/[^0-9A-Z]/g,''))
            .replace(/(.{{4}})/g,'$1 ').substring(2).trim();
    }};
    window.addEventListener('message', e => {{
        if(e.origin === location.origin && e.data === 'attached') {{
            document.querySelector('#AjouterRIB tbody').innerHTML='<tr><td>bank.pdf</td></tr>';
        }}
    }});
    function Page_ClientValidate() {{
            if ({json.dumps(scenario)} === 'contributions')
                document.querySelector('#new_isdomprincipale_1').checked=true;
        const iban=countryCode+partial.value.replaceAll(' ','');
        document.querySelector('#new_ibannumerocompte').value=iban;
        const numeric=(iban.slice(4)+iban.slice(0,4)).split('').map(c =>
            /[A-Z]/.test(c) ? (c.charCodeAt(0)-55).toString() : c).join('');
        return partial.value.length === partial.maxLength && BigInt(numeric)%97n===1n
            && document.querySelectorAll('#AjouterRIB tbody tr').length===1;
    }}
    function submitBank() {{
        if(!Page_ClientValidate()) return;
        fetch('/test-submit', {{method:'POST', body:JSON.stringify({{
            iban:document.querySelector('#new_ibannumerocompte').value,
            contributions:document.querySelector('#new_isdomprincipale_1').checked
        }})}}).then(() => location.href='{BANK_PATH}?success=true&type=bancaire');
    }}
    </script>
    """
    )
    modal = """
    <input type="file" id="AttachFile">
    <input type="button" id="InsertButton" disabled>
    <script>
    document.querySelector('#AttachFile').onchange=e =>
        fetch('/test-upload', {method:'POST',body:e.target.files[0]}).then(() => {
            document.querySelector('#InsertButton').disabled=false;
        });
    document.querySelector('#InsertButton').onclick=() =>
        fetch('/test-attach', {method:'POST'}).then(() => {
            parent.postMessage('attached',location.origin);
        });
    </script>
    """

    async def handler(route):
        from urllib.parse import urlsplit

        request = route.request
        url = urlsplit(request.url)
        if url.path in {"/test-submit", "/test-upload", "/test-attach"}:
            events.append((url.path, request.post_data_buffer))
            await route.fulfill(status=200, body="ok")
        elif url.path == ADD_PATH:
            await route.fulfill(
                content_type="text/html; charset=utf-8", body=second if url.query else first
            )
        elif url.path.startswith("/_portal/modal-form-template-path/"):
            await route.fulfill(content_type="text/html; charset=utf-8", body=modal)
        elif url.path == BANK_PATH:
            await route.fulfill(
                content_type="text/html; charset=utf-8",
                body=AUTH + (CONFIRMATION if scenario != "no_ack" else "Traitement"),
            )
        else:
            # All requests, including subresources, terminate here. No live network fallback.
            await route.fulfill(status=404, body="Unmapped test route")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        context = await browser.new_context()
        await context.route("**/*", handler)
        context.set_default_timeout(3000)

        @asynccontextmanager
        async def test_context():
            yield context

        runner = BankBrowser(None)
        runner.context = test_context
        stages = []
        draft = {
            "owner": OWNER,
            "bank": normalize_bank("Example Holder", IBAN, COUNTRY),
            "attachment": {"name": "bank.pdf"},
        }
        try:
            if scenario == "success":
                receipt = await runner.send(
                    draft, b"%PDF-1.4\nexample", lambda s, **v: stages.append(s)
                )
                assert receipt["activation"] == "pending_cfe_validation"
                assert [p for p, _ in events] == ["/test-upload", "/test-attach", "/test-submit"]
                assert json.loads(events[-1][1]) == {"iban": IBAN, "contributions": False}
                assert events[0][1] == b"%PDF-1.4\nexample"
            else:
                with pytest.raises(CfeError):
                    await runner.send(draft, b"%PDF-1.4\nexample", lambda s, **v: stages.append(s))
                if scenario in {"wrong_country", "wrong_attachment"}:
                    assert not events
                if scenario == "contributions":
                    assert [p for p, _ in events] == ["/test-upload", "/test-attach"]
        finally:
            await browser.close()
