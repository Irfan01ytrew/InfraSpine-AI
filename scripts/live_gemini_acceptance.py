"""Run one additive synthetic upload -> live Gemini cited answer -> source preview check.

This script does not create accounts, change provider settings, or delete uploaded data.
It intentionally leaves the private synthetic document in place for an administrator to review.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import sys
import time
from urllib.parse import urlparse

import httpx


PHRASE = "ORBIT-42"


def _pdf_bytes() -> bytes:
    stream = (
        "BT /F1 18 Tf 50 750 Td "
        "(Synthetic acceptance policy: the relay check phrase is ORBIT-42.) Tj ET"
    ).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(obj + b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


def _validate_base_url(value: str, allow_remote: bool) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("The base URL must be an HTTP(S) origin without embedded credentials.")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == "localhost"
    if not loopback and not allow_remote:
        raise ValueError("Remote targets require --allow-remote and HTTPS.")
    if not loopback and parsed.scheme != "https":
        raise ValueError("Remote acceptance targets must use HTTPS.")
    if parsed.query or parsed.fragment:
        raise ValueError("The base URL cannot include a query or fragment.")
    return value.rstrip("/")


def _step(response: httpx.Response, expected: set[int], label: str) -> None:
    if response.status_code not in expected:
        raise RuntimeError(f"{label} failed with HTTP {response.status_code}; response content was suppressed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Local API URL, or HTTPS isolated staging URL")
    parser.add_argument("--allow-remote", action="store_true", help="Permit a non-loopback HTTPS staging URL")
    args = parser.parse_args()
    try:
        base_url = _validate_base_url(args.base_url, args.allow_remote)
    except ValueError as exc:
        parser.error(str(exc))

    email = input("Synthetic acceptance account email: ").strip()
    password = getpass.getpass("Account password (hidden): ")
    if not email or not password:
        parser.error("An authorized account email and password are required.")

    headers = {"X-Requested-With": "InfraSpineAcceptance", "Accept": "application/json"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=httpx.Timeout(30, connect=10)) as client:
        response = client.post("/api/auth/login", json={"email": email, "password": password})
        _step(response, {200}, "Login")
        response = client.get("/api/auth/me")
        _step(response, {200}, "Authenticated account check")
        if response.json().get("role") != "admin":
            raise RuntimeError("Use a pre-created administrator account in the isolated staging deployment.")
        print("PASS: authenticated using the existing isolated staging administrator account.")

        response = client.post(
            "/api/rag/documents",
            data={"visibility": "private"},
            files={"file": ("infraspine-synthetic-gemini-check.pdf", _pdf_bytes(), "application/pdf")},
        )
        _step(response, {202}, "Synthetic PDF upload")
        document_id = response.json().get("id")
        if not isinstance(document_id, str):
            raise RuntimeError("Upload response did not include a document identifier.")
        print("PASS: synthetic private PDF was accepted; its identifier is retained locally in this process.")

        document = None
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status_response = client.get(f"/api/rag/documents/{document_id}")
            _step(status_response, {200}, "Document processing status")
            document = status_response.json()
            if document.get("status") in {"ready", "failed"}:
                break
            time.sleep(3)
        if not document or document.get("status") != "ready" or not document.get("searchable"):
            raise RuntimeError("Synthetic document did not become searchable within 180 seconds.")
        print("PASS: worker processing completed and the document is searchable.")

        response = client.post(
            "/api/rag/chat",
            json={
                "question": "According to the uploaded acceptance policy, what is the relay check phrase?",
                "document_ids": [document_id],
                "focus_document_ids": [document_id],
                "history": [],
            },
        )
        _step(response, {200}, "Live Gemini RAG question")
        answer = response.json()
        citations = answer.get("citations") or []
        if not str(answer.get("provider", "")).lower().startswith("gemini"):
            raise RuntimeError("The answer did not report Gemini as its provider.")
        matching = [citation for citation in citations if citation.get("document_id") == document_id and citation.get("available")]
        if not matching or PHRASE not in str(answer.get("answer", "")).upper():
            raise RuntimeError("The live answer did not return the synthetic fact with an available citation.")
        print("PASS: Gemini answered the synthetic question and cited the uploaded source.")

        citation = matching[0]
        page = citation.get("page_number")
        if not isinstance(page, int) or page < 1:
            raise RuntimeError("Citation did not contain a valid source page number.")
        response = client.get(f"/api/rag/documents/{document_id}/pages/{page}/image")
        _step(response, {200}, "Original source page preview")
        if not response.headers.get("content-type", "").lower().startswith("image/png") or not response.content:
            raise RuntimeError("The cited original source preview was not returned as a PNG image.")
        print("PASS: cited original-source page preview is readable under the same authenticated session.")
        print("The synthetic document was retained; remove it only through the normal reviewed UI after acceptance.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (httpx.HTTPError, RuntimeError) as exc:
        # Never print request/response content, which could contain credentials or document data.
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
