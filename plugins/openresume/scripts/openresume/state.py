"""Persist the active resume in the current project."""

import json
import re
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from .engine import parse_resume, render_resume
from .pdf_edit import BULLET_PREFIX, pdf_style, edit_pdf_text, PdfLayoutError

_PDF_HEADINGS = {
    "summary", "professional summary", "profile", "objective", "contact",
    "experience", "work experience", "professional experience", "employment history",
    "education", "skills", "technical skills", "core competencies", "projects",
    "selected projects", "certifications", "publications", "awards",
    "volunteer experience", "languages", "interests",
}
_PDF_PROSE_HEADINGS = {"summary", "professional summary", "profile", "objective"}
_PDF_EXPERIENCE_HEADINGS = {
    "experience", "work experience", "professional experience", "employment history",
    "volunteer experience",
}


class ResumeState:
    def __init__(self, directory: Path):
        self.directory = directory
        self.manifest = directory / "active.json"

    def parse(self, source: Path) -> dict:
        self.directory.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=self.directory) as temporary:
            snapshot = Path(temporary) / f"source{source.suffix.lower()}"
            snapshot.write_bytes(source.read_bytes())
            parsed = parse_resume(snapshot)
            snapshot.replace(self._snapshot(parsed))
            manifest = Path(temporary) / "active.json"
            manifest.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
            manifest.replace(self.manifest)
        return self._agent_view(parsed)

    def _load(self) -> dict:
        try:
            return json.loads(self.manifest.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValueError("No active resume. Run openresume parse <resume> first.") from None

    def view(self) -> dict:
        return self._agent_view(self._load())

    def _agent_view(self, parsed: dict, pdf_targets: dict | None = None) -> dict:
        view = {key: parsed[key] for key in ("format", "source_sha256")}
        if parsed["format"] == "pdf":
            sections = []
            pdf_lines = {}
            section = {"items": []}
            label = ""
            for page in parsed["pages"]:
                merged, markers = _associate_markers(page)
                for block in page["blocks"]:
                    block_items = []
                    previous_item = None
                    bullet_indent = None
                    previous_y = None
                    for index, line in enumerate(block["lines"], start=1):
                        identifier = f"{block['id']}/line-{index}"
                        if identifier in markers:
                            continue
                        line = merged.get(identifier, line)
                        text = "".join(span["text"] for span in line["spans"]).strip()
                        if not text:
                            continue
                        item = {
                            "id": f"{block['id']}/line-{index}",
                            "text": text,
                        }
                        candidate = " ".join(text.rstrip(":").casefold().split())
                        if candidate in _PDF_HEADINGS:
                            _append_pdf_items(section, label, block_items)
                            block_items = []
                            label = candidate
                            section = {"heading": item, "items": []}
                            sections.append(section)
                            pdf_lines[item["id"]] = [line]
                            previous_item = None
                            bullet_indent = None
                        else:
                            if not sections:
                                sections.append(section)
                            x, y = line["bbox"][:2]
                            prefix = BULLET_PREFIX.match(text)
                            is_bullet = prefix is not None
                            continuation = (
                                bullet_indent is not None
                                and 0 <= x - bullet_indent <= 24
                                and 0 < y - previous_y <= 1.6 * line["spans"][0]["size"]
                            )
                            if previous_item is not None and not is_bullet and (
                                label in _PDF_PROSE_HEADINGS or continuation
                            ):
                                previous_item["text"] += " " + text
                                pdf_lines[previous_item["id"]].append(line)
                            else:
                                item["kind"] = "bullet" if is_bullet else "paragraph"
                                if prefix:
                                    item["text"] = text[prefix.end():]
                                block_items.append(item)
                                pdf_lines[item["id"]] = [line]
                                previous_item = item
                                bullet_indent = x if is_bullet else None
                            previous_y = y
                    _append_pdf_items(section, label, block_items)
            for section in sections:
                items = [*section["items"]]
                if "heading" in section:
                    items.append(section["heading"])
                for entry in section.get("entries", []):
                    items.extend(entry["details"] + entry["bullets"])
                for item in items:
                    lines = pdf_lines[item["id"]]
                    if pdf_targets is not None and pdf_style(lines) is not None:
                        pdf_targets[item["id"]] = lines
            view["sections"] = sections
            view["warnings"] = list(parsed["warnings"])
            if sections and not any("heading" in section for section in sections):
                view["warnings"].append("No recognized section headings were found in the PDF.")
            return view

        sections = []
        section = None
        current_part = None
        for index, paragraph in enumerate(parsed["paragraphs"], start=1):
            if not paragraph["text"].strip():
                continue
            part = paragraph["id"].split("#", 1)[0]
            item = {
                "id": f"p{index}",
                "text": paragraph["text"],
                "style": paragraph["style"],
            }
            if paragraph["style"].lower().startswith("heading"):
                section = {"heading": item, "items": []}
                sections.append(section)
            else:
                if section is None or part != current_part:
                    section = {"items": []}
                    sections.append(section)
                section["items"].append(item)
            current_part = part
        view["sections"] = sections
        return view

    def render(
        self, output: Path, payload: dict | None = None, *, operations: list[dict] | None = None,
        layout: str = "fixed",
    ) -> Path:
        parsed = self._load()
        fingerprint = parsed["source_sha256"]
        if payload is not None:
            if payload["source_sha256"] != fingerprint:
                raise ValueError("The edits refer to a different resume. Review the active resume first.")
            if operations is not None:
                raise ValueError("Supply either an edit file or direct operations.")
            operations = payload.get("operations")
        return render_resume(
            self._snapshot(parsed),
            output,
            edits=payload.get("edits") if payload is not None else None,
            operations=self._resolve_operations(parsed, operations) if operations else None,
            source_sha256=fingerprint,
            layout=layout,
        )

    def preflight(self, payload: dict, *, layout="fixed") -> dict:
        """Perform the complete render in memory, without creating an output file."""
        from .engine import _edit_docx
        from hashlib import sha256
        import pymupdf

        parsed = self._load()
        try:
            if payload.get("source_sha256") != parsed["source_sha256"]:
                raise ValueError("The edits refer to a different resume.")
            data = self._snapshot(parsed).read_bytes()
            if sha256(data).hexdigest() != parsed["source_sha256"]:
                raise ValueError("The source changed since parsing. Parse it again before rendering.")
            operations = self._resolve_operations(parsed, payload.get("operations", []))
            if parsed["format"] == "pdf":
                if payload.get("edits"):
                    raise ValueError("Run edits require a DOCX source.")
                result = edit_pdf_text(data, operations, layout=layout)
                with pymupdf.open(stream=result, filetype="pdf") as document:
                    return {"valid": True, "layout": layout, "pages": len(document),
                            "links": sum(len(page.get_links()) for page in document), "failures": []}
            if layout != "fixed":
                raise ValueError("Reflow layout is only supported for PDF sources.")
            _edit_docx(data, payload.get("edits", {}), operations)
            return {"valid": True, "failures": []}
        except PdfLayoutError as error:
            return {"valid": False, "layout": layout, "failures": error.failures}
        except ValueError as error:
            return {"valid": False, "layout": layout, "failures": [{"reason": str(error)}]}

    def _resolve_operations(self, parsed: dict, operations: list) -> list[dict]:
        resolved, failures = [], []
        for index, operation in enumerate(operations, 1):
            try:
                items = self._resolve_operation(parsed, [operation])
                if operation.get("op") == "add_section" and parsed["format"] == "pdf":
                    items[0]["keep_with_next"] = True
                    items[1]["paragraph"] = True
                for item in items:
                    if parsed["format"] == "pdf":
                        item["operation_index"] = index
                resolved.extend(items)
            except ValueError as error:
                failures.append({"operation_index": index,
                                 "id": operation.get("id") if isinstance(operation, dict) else None,
                                 "action": operation.get("op") if isinstance(operation, dict) else None,
                                 "reason": str(error)})
        if failures:
            raise PdfLayoutError(failures)
        return resolved

    def _resolve_operation(self, parsed: dict, operations: list) -> list[dict]:
        expanded = []
        for operation in operations:
            if isinstance(operation, dict) and operation.get("op") == "add_section":
                if set(operation) != {"op", "id", "heading", "text", "heading_template_id", "template_id"}:
                    raise ValueError("add_section requires id, heading, text, heading_template_id and template_id.")
                expanded.extend([
                    {"op": "add", "id": operation["id"], "text": operation["heading"],
                     "template_id": operation["heading_template_id"]},
                    {"op": "add", "id": operation["id"], "text": operation["text"],
                     "template_id": operation["template_id"]},
                ])
            else:
                expanded.append(operation)
        operations = expanded
        is_pdf = parsed["format"] == "pdf"
        if is_pdf:
            targets = {}
            if operations:
                self._agent_view(parsed, targets)
            locations = targets
        else:
            locations = {
                f"p{index}": paragraph["id"]
                for index, paragraph in enumerate(parsed["paragraphs"], start=1)
                if paragraph["text"].strip()
            }
        resolved = []
        for operation in operations:
            if not isinstance(operation, dict) or operation.get("op") not in ("add", "replace", "remove", "format"):
                raise ValueError("Each operation must specify add, replace, remove, or format.")
            action = operation["op"]
            required = {"op", "id"} if action == "remove" else {"op", "id", "text"}
            allowed = required | ({"template_id"} if action == "add" else set())
            if action == "format":
                allowed |= {"match", "occurrence"}
            if not required <= operation.keys() <= allowed:
                raise ValueError(f"Invalid fields for the {action} operation.")
            item = dict(operation)
            for field in ("id", "template_id"):
                if field in item:
                    identifier = item[field]
                    if not isinstance(identifier, str) or identifier not in locations:
                        raise ValueError(f"Unknown editable resume ID: {identifier}")
                    item[field] = locations[identifier]
            if is_pdf:
                item["source_id"] = operation["id"]
                item["page"] = int(operation["id"].split("/", 1)[0].removeprefix("page-"))
                item["lines"] = item.pop("id")
                if "template_id" in item:
                    item["template_lines"] = item.pop("template_id")
                    item["template_page"] = int(operation["template_id"].split("/", 1)[0].removeprefix("page-"))
            resolved.append(item)
        return resolved

    def _snapshot(self, parsed: dict) -> Path:
        fingerprint = parsed["source_sha256"]
        file_format = parsed["format"]
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
            or file_format not in {"docx", "pdf"}
        ):
            raise ValueError("Invalid saved resume metadata.")
        return self.directory / f"{fingerprint}.{file_format}"


def _associate_markers(page):
    """Join only unambiguous, nearby marker/body lines on the same baseline."""
    lines = [(f"{block['id']}/line-{i}", line) for block in page["blocks"]
             for i, line in enumerate(block["lines"], 1)]
    merged, markers = {}, set()
    marker_pattern = re.compile(r"^(?:[•●◦▪▫‣⁃∙·\-–—*]|\d+[.)])$")
    for identifier, marker in lines:
        text = "".join(s["text"] for s in marker["spans"]).strip()
        if not marker_pattern.fullmatch(text) or tuple(marker["dir"]) != (1, 0):
            continue
        candidates = []
        for body_id, body in lines:
            body_text = "".join(s["text"] for s in body["spans"]).strip()
            if (body_id == identifier or body_id in merged or not body_text
                    or marker_pattern.fullmatch(body_text) or BULLET_PREFIX.match(body_text)):
                continue
            if (tuple(body["dir"]) == (1, 0)
                    and 0 <= body["bbox"][0] - marker["bbox"][2] <= 24
                    and abs(body["spans"][0]["origin"][1] - marker["spans"][0]["origin"][1]) <= 1.5):
                candidates.append((body_id, body))
        if len(candidates) == 1:
            body_id, body = candidates[0]
            combined = deepcopy(body)
            combined["spans"] = deepcopy(marker["spans"]) + combined["spans"]
            combined["spans"][len(marker["spans"]) - 1]["text"] += " "
            combined["bbox"] = [min(marker["bbox"][0], body["bbox"][0]),
                                min(marker["bbox"][1], body["bbox"][1]),
                                max(marker["bbox"][2], body["bbox"][2]),
                                max(marker["bbox"][3], body["bbox"][3])]
            combined["source_bboxes"] = [marker["bbox"], body["bbox"]]
            merged[body_id] = combined
            markers.add(identifier)
    return merged, markers


def _append_pdf_items(section: dict, label: str, items: list[dict]) -> None:
    if label in _PDF_EXPERIENCE_HEADINGS:
        for index, item in enumerate(items):
            if index > 0 and item["kind"] == "bullet" and all(
                bullet["kind"] == "bullet" for bullet in items[index:]
            ):
                section.setdefault("entries", []).append({
                    "details": items[:index],
                    "bullets": items[index:],
                })
                return
    section["items"].extend(items)
