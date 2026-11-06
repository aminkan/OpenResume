"""Read resume content and preserve the source document for rendering."""

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pymupdf
from docx import Document
from docx.oxml.ns import qn
from docx.opc.oxml import serialize_part_xml
from docx.text.paragraph import Paragraph

from .pdf_edit import edit_pdf_text
from .markup import parse_markup, format_range


def _format(path: Path) -> str:
    file_format = path.suffix.lower()
    if file_format not in {".docx", ".pdf"}:
        raise ValueError("Supported resume formats are DOCX and PDF.")
    return file_format[1:]


def _docx_paragraphs(document):
    for part in document.part.package.parts:
        if part is document.part or part.content_type in {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml",
        }:
            for element in part.element.iter(qn("w:p")):
                yield Paragraph(element, part)


def _location(part, element):
    return f"{part.partname}#{element.getroottree().getelementpath(element)}"


def _parse_docx(data: bytes) -> dict:
    document = Document(BytesIO(data))
    paragraphs = []
    for paragraph in _docx_paragraphs(document):
        # Keep each run separate so mixed formatting can survive targeted edits.
        runs = []
        for item in paragraph.iter_inner_content():
            if hasattr(item, "runs"):
                runs.extend(_run_data(run, item.url) for run in item.runs)
            else:
                runs.append(_run_data(item))
        paragraphs.append({
            "id": _location(paragraph.part, paragraph._p),
            "text": paragraph.text,
            "style": paragraph.style.name,
            "editable": _plain_paragraph(paragraph),
            "properties_xml": paragraph._p.pPr.xml if paragraph._p.pPr is not None else None,
            "runs": runs,
        })
    return {
        "paragraphs": paragraphs,
        "sections": [section._sectPr.xml for section in document.sections],
    }


def _run_data(run, hyperlink=None):
    return {
        "id": _location(run.part, run._r),
        "text": run.text,
        "hyperlink": hyperlink,
        "properties_xml": run._r.rPr.xml if run._r.rPr is not None else None,
    }


def _plain_paragraph(paragraph):
    element = paragraph._p
    parent = element.getparent()
    return (
        parent is not None
        and parent.tag in {qn("w:body"), qn("w:tc"), qn("w:hdr"), qn("w:ftr")}
        and element.find(".//" + qn("w:sectPr")) is None
        and all(child.tag in {qn("w:pPr"), qn("w:r")} for child in element)
        and all(
            child.tag in {qn("w:rPr"), qn("w:t")}
            for run in paragraph.runs for child in run._r
        )
    )


def _parse_pdf(data: bytes) -> dict:
    with pymupdf.open(stream=data, filetype="pdf") as document:
        if document.needs_pass:
            raise ValueError("The PDF is password protected.")
        pages = []
        warnings = []
        for page in document:
            number = page.number + 1
            layout = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT, sort=True)
            for block in layout["blocks"]:
                block["id"] = f"page-{number}/block-{block['number']}"
            if not any(
                span["text"].strip()
                for block in layout["blocks"]
                for line in block["lines"]
                for span in line["spans"]
            ):
                warnings.append(f"Page {number} has no extractable text and may require OCR.")
            pages.append({"number": number, "rotation": page.rotation, **layout})
        return {"pages": pages, "warnings": warnings}


def parse_resume(file_path: str | Path) -> dict:
    """Return JSON-compatible content and locations for a DOCX or PDF."""
    source = Path(file_path)
    file_format = _format(source)
    data = source.read_bytes()
    content = _parse_docx(data) if file_format == "docx" else _parse_pdf(data)
    return {"format": file_format, "source_sha256": sha256(data).hexdigest(), **content}


def render_resume(
    file_path: str | Path,
    output_path: str | Path,
    *,
    edits: dict[str, str] | None = None,
    operations: list[dict] | None = None,
    source_sha256: str | None = None,
    layout: str = "fixed",
) -> Path:
    """Render from the original file, optionally replacing plain DOCX runs."""
    source = Path(file_path)
    output = Path(output_path)
    file_format = _format(source)
    if _format(output) != file_format:
        raise ValueError("The output must use the same format as the source.")
    data = source.read_bytes()
    if layout not in {"fixed", "reflow"} or (file_format != "pdf" and layout != "fixed"):
        raise ValueError("Reflow layout is only supported for PDF sources.")
    if source_sha256 is not None and sha256(data).hexdigest() != source_sha256:
        raise ValueError("The source changed since parsing. Parse it again before rendering.")
    if edits and operations:
        raise ValueError("Use either run edits or paragraph operations.")
    if edits or operations or (file_format == "pdf" and layout == "reflow"):
        if source_sha256 is None:
            raise ValueError("Text replacement requires the source SHA-256 from parsing.")
        if file_format == "pdf":
            if edits:
                raise ValueError("Run edits require a DOCX source.")
            data = edit_pdf_text(data, operations or [], layout=layout)
        else:
            data = _edit_docx(data, edits or {}, operations or [])
    # Exclusive creation protects the original and any earlier render.
    try:
        with output.open("xb") as destination:
            destination.write(data)
    except FileExistsError:
        raise FileExistsError(f"Output already exists; choose a new path: {output}") from None
    return output


def _edit_docx(data: bytes, edits: dict[str, str], operations: list[dict]) -> bytes:
    document = Document(BytesIO(data))
    runs = {}
    paragraphs = {}
    for paragraph in _docx_paragraphs(document):
        paragraphs[_location(paragraph.part, paragraph._p)] = paragraph
        for item in paragraph.iter_inner_content():
            for run in item.runs if hasattr(item, "runs") else (item,):
                runs[_location(run.part, run._r)] = run
    changed_parts = {}
    for run_id, replacement in edits.items():
        if run_id not in runs:
            raise ValueError(f"Unknown DOCX run ID: {run_id}")
        run = runs[run_id]
        if any(child.tag not in {qn("w:rPr"), qn("w:t")} for child in run._r):
            raise ValueError(f"Run contains non-text elements: {run_id}")
        if not isinstance(replacement, str) or any(c in replacement for c in "\r\n\t"):
            raise ValueError("Replacement must be a string without line breaks or tabs.")
        characters = parse_markup(replacement)
        _append_characters(run._parent, run._r, characters, after=run._r)
        run._r.getparent().remove(run._r)
        changed_parts[run.part.partname.membername] = run.part

    insertion_points = {}
    for operation in operations:
        action = operation["op"]
        target = operation["id"]
        paragraph = paragraphs.get(target)
        if paragraph is None or not _plain_paragraph(paragraph):
            raise ValueError(f"Unsupported paragraph target: {target}")
        element = paragraph._p
        parent = element.getparent()
        if action == "remove":
            if parent.tag == qn("w:tc") and element is parent[-1]:
                raise ValueError("Cannot remove the final paragraph in a table cell.")
            parent.remove(element)
        elif action == "format":
            start, characters = format_range(paragraph.text, operation)
            _format_paragraph(paragraph, start, characters)
        elif action in {"replace", "add"}:
            text = operation["text"]
            template = paragraphs.get(operation.get("template_id", target))
            if template is None or not _plain_paragraph(template):
                raise ValueError("The template must be a plain-text paragraph.")
            if action == "add":
                if template.part is not paragraph.part:
                    raise ValueError("The template and insertion point must share a document part.")
                clone = deepcopy(template._p)
                for attribute in tuple(clone.attrib):
                    del clone.attrib[attribute]
                _replace_paragraph_text(Paragraph(clone, paragraph.part), text)
                after = insertion_points.get(target, element)
                after.addnext(clone)
                insertion_points[target] = clone
            else:
                _replace_paragraph_text(paragraph, text)
        else:
            raise ValueError(f"Unsupported operation: {action}")
        changed_parts[paragraph.part.partname.membername] = paragraph.part

    output = BytesIO()
    with ZipFile(BytesIO(data)) as source, ZipFile(output, "w") as destination:
        destination.comment = source.comment
        for entry in source.infolist():
            part = changed_parts.get(entry.filename)
            content = serialize_part_xml(part.element) if part is not None else source.read(entry)
            destination.writestr(entry, content)
    return output.getvalue()


def _replace_paragraph_text(paragraph, text):
    characters = parse_markup(text)
    runs = [run for run in paragraph.runs if run.text]
    formats = {str(run._r.rPr.xml) if run._r.rPr is not None else None for run in runs}
    if len(formats) > 1:
        raise ValueError("Mixed-format paragraphs require individual run edits.")
    if not runs:
        runs = [paragraph.add_run()]
    template = deepcopy(runs[0]._r)
    for run in paragraph.runs:
        paragraph._p.remove(run._r)
    _append_characters(paragraph, template, characters)


def _append_characters(paragraph, template, characters, after=None):
    from docx.text.run import Run

    previous = None
    run = None
    for character, bold, italic in characters:
        style = (bold, italic)
        if run is None or style != previous:
            element = deepcopy(template)
            if after is None:
                paragraph._p.append(element)
            else:
                after.addnext(element)
            after = element
            run = Run(element, paragraph)
            run.text = ""
            if bold is not None:
                run.bold = bold
            if italic is not None:
                run.italic = italic
            previous = style
        run.text += character


def _format_paragraph(paragraph, start, characters):
    end = start + len(characters)
    position = 0
    for run in list(paragraph.runs):
        text = run.text
        run_end = position + len(text)
        if position < end and run_end > start:
            styled = []
            for index, character in enumerate(text, position):
                styled.append(characters[index - start] if start <= index < end
                              else (character, None, None))
            _append_characters(paragraph, run._r, styled, after=run._r)
            paragraph._p.remove(run._r)
        position = run_end
