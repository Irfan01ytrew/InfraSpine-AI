"""Short-lived, owner-bound image artifacts used by chat image generation and search."""

from __future__ import annotations

import hashlib
import hmac
import io
import os
import re
import time
import uuid
from pathlib import Path

from fastapi import HTTPException

IMAGE_TTL_SECONDS = 24 * 60 * 60
MAX_INPUT_BYTES = 24 * 1024 * 1024
MAX_OUTPUT_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_USER_CACHE_BYTES = 250 * 1024 * 1024
MAX_USER_CACHE_FILES = 40
CHAT_IMAGE_TOKEN = re.compile(r"(?<![0-9a-f])([0-9a-f]{32}\.\d{10}\.[0-9a-f]{64})(?![0-9a-f])")


def latest_chat_image_token(answer: str) -> str | None:
    """Return a signed image artifact token from a saved assistant answer, if one is present."""
    matches = list(CHAT_IMAGE_TOKEN.finditer(answer))
    return matches[-1].group(1) if matches else None


def create_chat_image(data: bytes, root: Path, user_id: uuid.UUID, signing_key: bytes) -> str:
    """Decode and re-encode a bounded raster image, then save it in the requesting user's private cache."""
    normalized = _sanitize_image(data)
    base = root.resolve()
    owner_dir = base / str(user_id)
    owner_dir.mkdir(parents=True, exist_ok=True)
    if owner_dir.resolve().parent != base:
        raise ValueError("Image cache directory is not contained by its configured root")
    _cleanup(owner_dir)

    now = int(time.time())
    expires = now + IMAGE_TTL_SECONDS
    image_id = uuid.uuid4().hex
    final_path = owner_dir / f"{image_id}.png"
    temporary = owner_dir / f"{image_id}.png.tmp"
    try:
        with temporary.open("xb") as output:
            output.write(normalized)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, final_path)
    finally:
        temporary.unlink(missing_ok=True)

    signature = _signature(image_id, expires, user_id, signing_key)
    return f"{image_id}.{expires}.{signature}"


def resolve_chat_image(token: str, root: Path, user_id: uuid.UUID, signing_key: bytes) -> Path:
    """Verify expiry, ownership and path containment before returning a generated image."""
    match = re.fullmatch(r"([0-9a-f]{32})\.(\d{10})\.([0-9a-f]{64})", token)
    if not match:
        raise HTTPException(status_code=404, detail="Image not found or expired.")
    image_id, expires_text, supplied = match.groups()
    expires = int(expires_text)
    expected = _signature(image_id, expires, user_id, signing_key)
    if expires < int(time.time()) or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=404, detail="Image not found or expired.")

    base = root.resolve()
    owner_dir = (base / str(user_id)).resolve()
    path = (owner_dir / f"{image_id}.png").resolve()
    if owner_dir.parent != base or path.parent != owner_dir or not path.is_file() or path.stat().st_size > MAX_OUTPUT_BYTES:
        raise HTTPException(status_code=404, detail="Image not found or expired.")
    return path


def read_chat_image(token: str, root: Path, user_id: uuid.UUID, signing_key: bytes) -> bytes:
    """Read an owner-bound image after verifying its signature, expiry and bounded size."""
    path = resolve_chat_image(token, root, user_id, signing_key)
    return path.read_bytes()


def _signature(image_id: str, expires: int, user_id: uuid.UUID, key: bytes) -> str:
    message = f"chat-image:{image_id}:{expires}:{user_id}".encode("ascii")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _sanitize_image(data: bytes) -> bytes:
    if not data or len(data) > MAX_INPUT_BYTES:
        raise ValueError("Image output is empty or exceeds the image size limit")
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("Only PNG, JPEG and WebP images can be returned in chat")
            if source.width < 1 or source.height < 1 or source.width * source.height > MAX_IMAGE_PIXELS:
                raise ValueError("Image dimensions exceed the chat image limit")
            source.load()
            image = ImageOps.exif_transpose(source)
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=False)
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("The image provider returned an invalid raster image") from exc
    body = output.getvalue()
    if not body or len(body) > MAX_OUTPUT_BYTES:
        raise ValueError("The sanitized image exceeds the chat image size limit")
    return body


def _cleanup(owner_dir: Path) -> None:
    now = time.time()
    files: list[tuple[float, int, Path]] = []
    try:
        for path in owner_dir.iterdir():
            try:
                if not path.is_file() or path.suffix != ".png":
                    continue
                stat = path.stat()
                if stat.st_mtime < now - IMAGE_TTL_SECONDS:
                    path.unlink(missing_ok=True)
                else:
                    files.append((stat.st_mtime, stat.st_size, path))
            except OSError:
                continue
    except OSError:
        return
    files.sort()
    total = sum(size for _, size, _ in files)
    while files and (len(files) > MAX_USER_CACHE_FILES or total > MAX_USER_CACHE_BYTES):
        _, size, oldest = files.pop(0)
        try:
            oldest.unlink(missing_ok=True)
            total -= size
        except OSError:
            continue
