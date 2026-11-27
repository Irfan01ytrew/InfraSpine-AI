"""PNG and JPEG documents.

An image has no text of its own. The parser checks and decodes it (in the parser process, under its memory
limit) and hands it to the worker as one picture; the worker has the active vision-capable provider describe
it (`app.rag.image_descriptions`), and that description is what is indexed and cited. Citations name the
image itself ("Image"), never an invented page number.
"""

import io
from pathlib import Path

from app.rag.parsers.base import ExtractedImage, Locator, ParseError, ParseLimits, Section

IMAGE_FORMATS = frozenset({"png", "jpeg", "webp"})
_PIL_FORMATS = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}
MAX_SIDE = 1600  # enough to read a chart or screenshot; keeps provider requests small


def image_locator() -> Locator:
    return Locator("image", 1, "Image", {"image": 1})


def _invalid() -> ParseError:
    return ParseError("invalid_image", "The image could not be read. It may be damaged.")


def _open(path: Path, file_format: str):
    """Open an image from its header only (no pixels decoded) and check its format and dimensions."""
    from PIL import Image, UnidentifiedImageError

    from app.core.config import get_settings

    settings = get_settings()
    try:
        image = Image.open(path)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        raise _invalid() from None
    if image.format != _PIL_FORMATS[file_format]:
        image.close()
        raise ParseError("file_type_mismatch", f"The file's content does not match its .{file_format} extension.")
    width, height = image.size
    if width * height > settings.rag_image_max_megapixels * 1_000_000:
        image.close()
        raise ParseError(
            "image_too_large", f"The image is larger than {settings.rag_image_max_megapixels} megapixels."
        )
    if min(width, height) < settings.rag_image_min_pixels:
        image.close()
        raise ParseError("image_too_small", "The image is too small to be described.")
    return image


class _ImageParser:
    file_format = ""
    extensions: tuple[str, ...] = ()

    def inspect(self, path: Path, limits: ParseLimits) -> int:
        """Upload-time check: a well-formed image within the size limits. One unit: the image itself."""
        image = _open(path, self.file_format)
        try:
            image.verify()  # structure and checksums, without decoding the pixels
        except Exception:
            raise _invalid() from None
        finally:
            image.close()
        return 1

    def parse(self, path: Path, limits: ParseLimits) -> list[Section]:
        """Extract on-screen or scanned text using local RapidOCR, supplemented by provider descriptions."""
        from PIL import Image
        import numpy as np

        try:
            from app.rag.ocr import _local_engine

            image = Image.open(path).convert("RGB")
            arr = np.ascontiguousarray(np.array(image))
            engine = _local_engine()
            result = engine(arr)
            lines = [str(t).strip() for t in (getattr(result, "txts", None) or ()) if str(t).strip()]
            ocr_text = "\n".join(lines)
            if ocr_text:
                return [Section(ocr_text[:limits.max_chars], image_locator())]
        except Exception:
            pass
        return []

    def picture(self, path: Path) -> ExtractedImage:
        """The image decoded, upright and downscaled, as PNG bytes for the provider."""
        from PIL import ImageOps

        image = _open(path, self.file_format)
        try:
            image.draft("RGB", (MAX_SIDE, MAX_SIDE))  # JPEG: decode directly at a reduced size
            upright = ImageOps.exif_transpose(image)
            rgb = upright.convert("RGB")
            rgb.thumbnail((MAX_SIDE, MAX_SIDE))
            buffer = io.BytesIO()
            rgb.save(buffer, format="PNG")
        except ParseError:
            raise
        except Exception:
            raise _invalid() from None
        finally:
            image.close()
        return ExtractedImage(None, buffer.getvalue())


def preview_png(path: Path, file_format: str, width: int) -> bytes:
    """An uploaded image as PNG at most `width` pixels wide, for showing it to readers of the document."""
    from PIL import ImageOps

    image = _open(path, file_format)
    try:
        image.draft("RGB", (width, width * 4))
        rgb = ImageOps.exif_transpose(image).convert("RGB")
        rgb.thumbnail((width, width * 4))
        buffer = io.BytesIO()
        rgb.save(buffer, format="PNG")
    except Exception:
        raise _invalid() from None
    finally:
        image.close()
    return buffer.getvalue()


class PngParser(_ImageParser):
    file_format = "png"
    extensions = (".png",)


class JpegParser(_ImageParser):
    file_format = "jpeg"
    extensions = (".jpg", ".jpeg")


class WebpParser(_ImageParser):
    file_format = "webp"
    extensions = (".webp",)
