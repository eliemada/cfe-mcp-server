"""Extract request shapes from a local HAR without exporting personal values."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

PORTAL_HOST = "monespaceperso.cfe.fr"
GUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", re.I)


def redacted_path(path: str) -> str:
    path = GUID.sub("{id}", path)
    return re.sub(r"(?<![A-Za-z])\d{6,}(?![A-Za-z])", "{number}", path)


def value_shape(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "nested"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return [value_shape(value[0], depth + 1)] if value else []
    if isinstance(value, dict):
        return {redacted_path(k): value_shape(v, depth + 1) for k, v in value.items()}
    return "unknown"


def catalog_har(har: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep portal API metadata only; omit auth traffic, cookies and all values."""
    endpoints: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in har.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        url = urlsplit(request.get("url", ""))
        if url.hostname != PORTAL_HOST:
            continue
        resource_type = entry.get("_resourceType", "")
        if not (
            resource_type in {"fetch", "xhr"}
            or url.path.startswith(("/_api/", "/_services/", "/_layout/"))
        ):
            continue
        path = redacted_path(url.path)
        method = request.get("method", "GET")
        key = method, path
        response = entry.get("response", {})
        post = request.get("postData", {})
        item: dict[str, Any] = {
            "method": method,
            "path": path,
            "query_fields": sorted({k for k, _ in parse_qsl(url.query)}),
            "status": response.get("status"),
            "content_type": response.get("content", {}).get("mimeType", ""),
        }
        if "json" in post.get("mimeType", ""):
            try:
                item["request_shape"] = value_shape(json.loads(post.get("text", "")))
            except (ValueError, TypeError):
                item["request_shape"] = "unavailable"
        elif post.get("params"):
            item["form_fields"] = sorted({p["name"] for p in post["params"]})
        content = response.get("content", {})
        if "json" in content.get("mimeType", "") and content.get("encoding") != "base64":
            try:
                item["response_shape"] = value_shape(json.loads(content.get("text", "")))
            except (ValueError, TypeError):
                pass
        endpoints[key] = item
    return sorted(endpoints.values(), key=lambda item: (item["path"], item["method"]))
