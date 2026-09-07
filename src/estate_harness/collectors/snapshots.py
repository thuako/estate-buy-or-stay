from __future__ import annotations

import hashlib
import ipaddress
import socket
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from estate_harness.store import atomic_json


def check_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or any(char.isspace() for char in url)
    ):
        raise ValueError("Source must be a public HTTP(S) URL without credentials")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
        raise ValueError("Local/private source hosts are not accepted")


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def collect_source(url: str, directory: Path, *, max_bytes: int = 20_000_000) -> dict:
    """Save exact response bytes and SHA-256; original support still requires an auditor.

    No arbitrary API credentials, browser cookies, or external policy assumptions
    are sent. This is an explicit CLI collector, not an agent-controlled URL executor.
    """
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "original_url": url,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "status": "unavailable",
        "content_hash": None,
        "path": None,
        "published_at": None,
        "revision_id": None,
    }
    try:
        check_public_url(url)
        opener = urllib.request.build_opener(PublicRedirect)
        request = urllib.request.Request(url, headers={"User-Agent": "estate-research-harness/0.1"})
        with opener.open(request, timeout=20) as response:
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("Source exceeds snapshot size limit")
            content_hash = hashlib.sha256(data).hexdigest()
            target = directory / f"{content_hash}.bin"
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(target)
            manifest.update(
                status="retrieved",
                content_hash=content_hash,
                path=str(target),
                final_url=response.url,
                content_type=response.headers.get("Content-Type"),
                revision_id=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
                bytes=len(data),
            )
    except urllib.error.HTTPError as error:
        manifest.update(status="not_found" if error.code == 404 else "unavailable", http_status=error.code)
    except (urllib.error.URLError, OSError, ValueError) as error:
        manifest["error"] = str(error)
    key = hashlib.sha256((url + manifest["retrieved_at"]).encode()).hexdigest()
    atomic_json(directory / f"{key}.manifest.json", manifest)
    return manifest
