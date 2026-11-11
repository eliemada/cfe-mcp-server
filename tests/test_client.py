import base64
import json
import stat

import httpx
import pytest

from cfe_mcp.client import (
    AuthenticationRequired,
    CfeClient,
    CfeError,
    is_authenticated_html,
    parse_claim_options,
    parse_messaging_options,
    portal_url,
)
from cfe_mcp.storage import read_private_json, write_private_json

AUTH_HTML = "<script>var isPortalUserLoggedIn = 'True';</script>"


@pytest.fixture
def state_path(tmp_path):
    path = tmp_path / "browser-state.json"
    write_private_json(
        path,
        {
            "cookies": [
                {
                    "name": ".AspNet.ApplicationCookie",
                    "value": "test-only-session",
                    "domain": "monespaceperso.cfe.fr",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "expires": -1,
                },
                {
                    "name": "unrelated",
                    "value": "must-not-send",
                    "domain": "example.com",
                    "path": "/",
                },
            ],
            "origins": [],
        },
    )
    return path


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/_api/contacts",
        "//evil.example/path",
        "///evil.example/path",
        "http://monespaceperso.cfe.fr/_api/contacts",
        "https://monespaceperso.cfe.fr@evil.example/",
        "https://monespaceperso.cfe.fr:444/",
        "/%2f/evil.example",
        "/\\evil.example",
        "/foo\r\nCookie: evil",
    ],
)
def test_session_origin_restriction(url):
    with pytest.raises(CfeError):
        portal_url(url)


def test_authentication_is_explicit():
    assert is_authenticated_html(AUTH_HTML)
    assert is_authenticated_html("var isPortalUserLoggedIn = true;")
    assert not is_authenticated_html("var isPortalUserLoggedIn = 'False';")
    assert not is_authenticated_html("Sign in to your account")


@pytest.mark.parametrize("collection,route", [
    ("reimbursements", "/mes-remboursements/"),
    ("messages", "/messagerie/message-recu/"),
])
async def test_grid_reuses_server_configuration_and_refreshes_csrf(state_path, collection, route):
    calls = []
    layout = [
        {
            "Id": "view",
            "Base64SecureConfiguration": "signed-by-cfe",
            "SortExpression": "createdon DESC",
        }
    ]
    encoded = base64.b64encode(json.dumps(layout).encode()).decode()
    html = AUTH_HTML + (
        '<div class="entity-grid" data-selected-view="view" '
        'data-get-url="/_services/entity-grid-data.json/site" '
        f'data-view-layouts="{encoded}"></div>'
    )

    def handler(request):
        calls.append(request)
        assert "must-not-send" not in request.headers.get("cookie", "")
        if request.url.path == route:
            return httpx.Response(200, text=html)
        if request.url.path == "/_layout/tokenhtml":
            return httpx.Response(
                200,
                text='<input name="__RequestVerificationToken" value="fresh-token">',
                headers={"Set-Cookie": "__RequestVerificationToken=fresh-cookie; Path=/; Secure"},
            )
        assert request.method == "POST"
        payload = json.loads(request.content)
        assert payload["base64SecureConfiguration"] == "signed-by-cfe"
        assert payload["page"] == 2
        assert payload["pagingCookie"] == "opaque-cursor"
        assert payload["pageSize"] == 20
        assert request.headers["__RequestVerificationToken"] == "fresh-token"
        assert "fresh-cookie" in request.headers["cookie"]
        return httpx.Response(
            200,
            json={
                "Records": [
                    {
                        "Id": "example",
                        "EntityName": "new_remboursement",
                        "Attributes": [
                            {
                                "Name": "new_montanttotal",
                                "Value": {"Value": 42.5},
                                "FormattedValue": "42,50 €",
                            },
                        ],
                    }
                ],
                "MoreRecords": True,
                "NextPagePagingCookie": "next-cursor",
                "PageNumber": 2,
            },
        )

    async with CfeClient(state_path, transport=httpx.MockTransport(handler)) as client:
        result = await client.grid(collection, 2, 20, "opaque-cursor")
    assert len(calls) == 3
    assert result["records"][0]["fields"]["new_montanttotal"]["value"] == 42.5
    assert result["next_paging_cookie"] == "next-cursor"
    assert result["has_more"] is True
    assert all(
        c["domain"] == "monespaceperso.cfe.fr" for c in read_private_json(state_path)["cookies"]
    )
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


async def test_expired_session_never_follows_redirect(state_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://login.example/"})

    async with CfeClient(state_path, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AuthenticationRequired):
            await client.session_status()
    assert len(calls) == 1


async def test_post_is_not_retried_and_error_contains_no_body(state_path):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("sensitive upstream text", request=request)

    async with CfeClient(state_path, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CfeError) as error:
            await client.request("POST", "/_api/example", json={"secret": "private"})
    assert len(calls) == 1
    assert "sensitive" not in str(error.value)
    assert "private" not in str(error.value)


def test_claim_options_follow_eligibility_and_kind():
    html = """
    <script>
    benefsFr_Etr.set('eligible','100000001');
    benefsFr_Etr.set('excluded','100000002');
    var nbPJMaxETR = 10; var nbPJMaxFR = 5;
    var nbSoinsMaxETR = 5; var nbSoinsMaxFR = 1;
    </script>
    <select id="new_contactbeneficiaire">
      <option value="">Sélectionner</option>
      <option value="eligible">Exemple 1999999999999</option>
      <option value="excluded">Inéligible</option>
    </select>
    <select id="new_paysdesoins">
      <option value="fr">FRANCE</option><option value="jp">JAPON</option>
    </select>
    <select id="new_circonstance"><option value="100000000">Maladie</option></select>
    <input id="EntityFormView_EntityID" value="">
    """
    foreign = parse_claim_options(html, "etr")
    assert foreign["beneficiaries"] == [{"id": "eligible", "label": "Exemple"}]
    assert foreign["countries"] == [{"id": "jp", "label": "JAPON"}]
    assert foreign["limits"] == {"attachments": 10, "care_items": 5}
    domestic = parse_claim_options(html, "fra")
    assert domestic["countries"] == [{"id": "fr", "label": "FRANCE"}]
    assert domestic["limits"] == {"attachments": 5, "care_items": 1}


def test_missing_form_fields_fail_clearly():
    with pytest.raises(CfeError):
        parse_claim_options("<html>Login page</html>", "etr")


MESSAGE_HTML = AUTH_HTML + """
<input id="__VIEWSTATE" value="private-state-not-for-tools">
<input id="EntityFormView_EntityID" value="">
<select id="new_theme">
  <option value=""></option>
  <option value="100000000">Signaler une anomalie sur un remboursement perçu : Soins</option>
</select>
<select id="new_formulaireportaildelincident">
  <option value="100000032">Ecrire un message - Remboursement Soin</option>
</select>
<input id="NextButton" type="button" value="Suivant">
<script>
$('#new_theme').on('input', function() {
  switch($(this).val()) {
    case '100000000': formValue = 100000032; break;
  }
  $('#new_formulaireportaildelincident').val(formValue);
});
</script>
"""


async def test_messaging_options_only_gets_initial_page_and_never_exposes_state(state_path):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path == "/messagerie/ecrire-message/"
        return httpx.Response(200, text=MESSAGE_HTML)

    async with CfeClient(state_path, transport=httpx.MockTransport(handler)) as client:
        result = await client.messaging_options()
    assert len(calls) == 1
    assert result["reimbursement_review"]["form_id"] == "100000032"
    assert result["existing_remote_draft"] is False
    assert result["remote_submission_supported"] is True
    assert result["submission_tool"] == "cfe_submit_review"
    assert result["supported_submission_themes"] == ["100000000"]
    assert "private-state-not-for-tools" not in json.dumps(result)


@pytest.mark.parametrize("html", [
    "<html>Login page</html>",
    MESSAGE_HTML.replace("case '100000000'", "case '100000099'"),
    MESSAGE_HTML.replace('value="100000032"', 'value="100000099"'),
    MESSAGE_HTML.replace("100000032", "100000033"),
])
def test_messaging_options_reject_missing_or_changed_mapping(html):
    with pytest.raises(CfeError):
        parse_messaging_options(html)
