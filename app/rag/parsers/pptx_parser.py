"""PowerPoint presentations (.pptx). One section per slide: title, text boxes, tables and speaker notes."""

from pathlib import Path

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from app.rag.parsers.base import Locator, ParseError, ParseLimits, Section, TextBudget, require_text


def _open(path: Path):
    try:
        return Presentation(str(path))
    except Exception:
        raise ParseError("invalid_file", "The presentation could not be read. It may be damaged.") from None


def _shape_texts(shapes, skip_id: int | None) -> list[str]:
    texts: list[str] = []
    for shape in shapes:
        if shape.shape_id == skip_id:
            continue
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            texts.extend(_shape_texts(shape.shapes, skip_id))
        elif shape.has_text_frame:
            text = shape.text_frame.text.strip()
            if text:
                texts.append(text)
        elif getattr(shape, "has_table", False) and shape.has_table:
            for row in shape.table.rows:
                cells = [" ".join(cell.text.split()) for cell in row.cells]
                if any(cells):
                    texts.append(" | ".join(c for c in cells if c))
    return texts


class PptxParser:
    file_format = "pptx"
    extensions = (".pptx",)

    def inspect(self, path: Path, limits: ParseLimits) -> int | None:
        count = len(_open(path).slides)
        if count > limits.max_units:
            raise ParseError("too_many_slides", f"The presentation has {count} slides; the limit is {limits.max_units}.")
        return count

    def parse(self, path: Path, limits: ParseLimits) -> list[Section]:
        presentation = _open(path)
        if len(presentation.slides) > limits.max_units:
            raise ParseError("too_many_slides", f"The presentation has more than {limits.max_units} slides.")
        budget = TextBudget(limits.max_chars)
        sections: list[Section] = []
        try:
            for number, slide in enumerate(presentation.slides, start=1):
                title_shape = slide.shapes.title
                title = " ".join(title_shape.text_frame.text.split()) if title_shape is not None else ""
                parts = _shape_texts(slide.shapes, title_shape.shape_id if title_shape is not None else None)
                if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                    notes = slide.notes_slide.notes_text_frame.text.strip()
                    if notes:
                        parts.append(f"Speaker notes: {notes}")
                if not title and not parts:
                    continue
                heading = f"Slide {number}" + (f": {title}" if title else "")
                text = budget.take(heading + ("\n" + "\n".join(parts) if parts else ""))
                label = f"Slide {number}" + (f" – {title}" if title else "")
                sections.append(Section(text, Locator("slide", number, label, {"slide": number, "title": title})))
        except ParseError:
            raise
        except Exception:
            raise ParseError("invalid_file", "The presentation could not be read. It may be damaged.") from None
        return require_text(sections)
