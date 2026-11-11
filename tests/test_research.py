import json

from cfe_mcp.research import catalog_har


def test_catalog_excludes_auth_and_values():
    har = {
        "log": {
            "entries": [
                {
                    "_resourceType": "xhr",
                    "request": {
                        "url": "https://cfeb2cprod.b2clogin.com/verify",
                        "method": "POST",
                        "postData": {"mimeType": "application/json", "text": '{"code":"123456"}'},
                    },
                },
                {
                    "_resourceType": "xhr",
                    "request": {
                        "url": "https://monespaceperso.cfe.fr/_api/contacts(11111111-1111-1111-1111-111111111111)?email=private@example.com",
                        "method": "POST",
                        "headers": [{"name": "Cookie", "value": "session-secret"}],
                        "postData": {
                            "mimeType": "application/json",
                            "text": '{"medical":"private-note"}',
                        },
                    },
                    "response": {
                        "status": 200,
                        "content": {"mimeType": "application/json", "text": '{"id":"private-id"}'},
                    },
                },
            ]
        }
    }
    result = catalog_har(har)
    rendered = json.dumps(result)
    assert len(result) == 1
    assert result[0]["path"] == "/_api/contacts({id})"
    for secret in ["123456", "private@example.com", "session-secret", "private-note", "private-id"]:
        assert secret not in rendered
    assert result[0]["request_shape"] == {"medical": "string"}
