from zipfile import ZipFile
import json

import pymupdf
import pytest
from docx import Document

from openresume import main, parse_resume, render_resume
from openresume.state import ResumeState


def test_docx_round_trip_and_run_replacement(tmp_path):
    source = tmp_path / "resume.docx"
    document = Document()
    document.sections[0].header.paragraphs[0].text = "Jane Candidate"
    bullet = document.add_paragraph(style="List Bullet")
    bullet.add_run("Built ").bold = True
    bullet.add_run("reporting tools")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "Python"
    document.save(source)

    parsed = parse_resume(source)
    assert {p["text"] for p in parsed["paragraphs"]} >= {
        "Jane Candidate", "Built reporting tools", "Python",
    }
    copy = render_resume(source, tmp_path / "copy.docx")
    assert copy.read_bytes() == source.read_bytes()

    bullet_data = next(p for p in parsed["paragraphs"] if p["style"] == "List Bullet")
    updated = render_resume(
        source,
        tmp_path / "updated.docx",
        edits={bullet_data["runs"][1]["id"]: "automated reports"},
        source_sha256=parsed["source_sha256"],
    )
    result = Document(updated)
    assert result.paragraphs[0].text == "Built automated reports"
    assert result.paragraphs[0].runs[0].bold is True
    assert result.paragraphs[0].style.name == "List Bullet"
    assert result.tables[0].cell(0, 0).text == "Python"
    assert result.sections[0].header.paragraphs[0].text == "Jane Candidate"
    with ZipFile(source) as original, ZipFile(updated) as edited:
        assert original.namelist() == edited.namelist()
        assert [
            name for name in original.namelist() if original.read(name) != edited.read(name)
        ] == ["word/document.xml"]

    with pytest.raises(ValueError, match="source changed"):
        render_resume(updated, tmp_path / "stale.docx", source_sha256=parsed["source_sha256"])
    with pytest.raises(FileExistsError):
        render_resume(source, source)


def test_pdf_round_trip_and_empty_text_warning(tmp_path):
    source = tmp_path / "resume.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Jane Candidate")
        page.insert_text((72, 100), "EXPERIENCE")
        page.insert_text((72, 120), "Built automated reports")
        document.new_page()
        document.save(source)

    parsed = parse_resume(source)
    span = parsed["pages"][0]["blocks"][0]["lines"][0]["spans"][0]
    assert span["text"] == "Jane Candidate"
    assert len(span["bbox"]) == 4
    assert parsed["warnings"] == ["Page 2 has no extractable text and may require OCR."]
    assert render_resume(source, tmp_path / "copy.pdf").read_bytes() == source.read_bytes()
    state = ResumeState(tmp_path / "state")
    view = state.parse(source)
    section = view["sections"][1]
    assert section["heading"]["text"] == "EXPERIENCE"
    assert section["items"][0]["text"] == "Built automated reports"
    assert "editable" not in section["items"][0]
    payload = {
        "source_sha256": view["source_sha256"],
        "operations": [
            {"op": "replace", "id": section["items"][0]["id"], "text": "Built reports"},
        ],
    }
    output = state.render(tmp_path / "edited.pdf", payload)
    with pymupdf.open(output) as edited:
        text = edited[0].get_text()
        assert "Built reports" in text
        assert "automated" not in text
        assert "Jane Candidate" in text and "EXPERIENCE" in text
    payload["operations"][0]["text"] = "Too long " * 50
    with pytest.raises(ValueError, match="does not fit"):
        state.render(tmp_path / "overflow.pdf", payload)

    payload["operations"] = [
        {"op": "add", "id": section["items"][0]["id"],
         "template_id": section["heading"]["id"], "text": "SKILLS"},
        {"op": "remove", "id": section["items"][0]["id"]},
    ]
    output = state.render(tmp_path / "add-remove.pdf", payload)
    with pymupdf.open(output) as edited:
        text = edited[0].get_text()
        assert "SKILLS" in text and "Built automated reports" not in text
        assert "Jane Candidate" in text and "EXPERIENCE" in text
    payload["operations"] = [
        {"op": "add", "id": section["heading"]["id"], "text": "SKILLS"},
    ]
    with pytest.raises(ValueError, match="clear space"):
        state.render(tmp_path / "overlap.pdf", payload)
    assert not (tmp_path / "overlap.pdf").exists()
    assert "pages" not in view
    assert "source_ids" not in json.dumps(view)


def test_shorter_pdf_bullet_closes_unused_lines(tmp_path):
    source = tmp_path / "resume.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 100), "- Built and maintained automated reporting tools\n"
                         "  for internal analytics and operations teams\n"
                         "- Shipped dashboards")
        document.save(source)

    state = ResumeState(tmp_path / "state")
    view = state.parse(source)
    bullet, following = view["sections"][0]["items"]
    assert following["text"] == "Shipped dashboards"
    output = state.render(tmp_path / "edited.pdf", operations=[
        {"op": "replace", "id": bullet["id"], "text": "Built automated reports for analysts."},
    ])

    with pymupdf.open(output) as document:
        lines = [line for block in document[0].get_text("dict", sort=True)["blocks"]
                 for line in block["lines"]]
        text = ["".join(span["text"] for span in line["spans"]) for line in lines]
        assert len(text) == 2
        assert text[0] == "- Built automated reports for analysts."
        assert text[1] == "- Shipped dashboards"
        assert lines[1]["spans"][0]["origin"][1] - lines[0]["spans"][0]["origin"][1] == pytest.approx(15.114, abs=0.05)


def test_cli_uses_saved_resume(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def invoke(*arguments):
        monkeypatch.setattr("sys.argv", ["openresume", *arguments])
        main()
        return json.loads(capsys.readouterr().out)

    with pytest.raises(SystemExit) as error:
        invoke("render", "output.docx")
    assert error.value.code == 1
    assert "No active resume" in capsys.readouterr().err

    document = Document()
    document.add_heading("Experience", level=1)
    document.add_paragraph("Built reporting tools")
    document.add_paragraph("Remove this line")
    document.save("resume.docx")
    source = tmp_path / "resume.docx"
    original = source.read_bytes()
    parsed = invoke("parse", source.name)
    assert invoke("view") == parsed
    section = parsed["sections"][0]
    assert section["heading"]["text"] == "Experience"
    assert "properties_xml" not in json.dumps(parsed)
    invoke("edit", "replace", section["items"][0]["id"], "--text", "Built reports",
           "--output", "direct.docx")
    assert Document("direct.docx").paragraphs[1].text == "Built reports"
    source.unlink()
    invoke("render", "output.docx")
    assert (tmp_path / "output.docx").read_bytes() == original

    edits = {
        "source_sha256": parsed["source_sha256"],
        "operations": [
            {"op": "replace", "id": section["items"][0]["id"], "text": "Built automated reports"},
            {"op": "add", "id": section["items"][0]["id"],
             "template_id": section["heading"]["id"], "text": "Projects"},
            {"op": "remove", "id": section["items"][1]["id"]},
        ],
    }
    (tmp_path / "edits.json").write_text(json.dumps(edits), encoding="utf-8")
    invoke("render", "tailored.docx", "--edits", "edits.json")
    paragraphs = Document("tailored.docx").paragraphs
    assert [p.text for p in paragraphs] == ["Experience", "Built automated reports", "Projects"]
    assert paragraphs[0].style.name == paragraphs[2].style.name == "Heading 1"
