import pymupdf
import pytest
from docx import Document

from openresume.markup import parse_markup
from openresume.state import ResumeState
from openresume import main


def test_nested_markup_and_escapes():
    assert parse_markup(r"A \b{B \i{C}} D") == [
        ("A", None, None), (" ", None, None), ("B", True, None),
        (" ", True, None), ("C", True, True), (" ", None, None), ("D", None, None),
    ]
    assert "".join(c for c, *_ in parse_markup(r"\{x\} \\path")) == "{x} \\path"


@pytest.mark.parametrize("text", [r"\b{oops", "oops}", r"\x{bad}", r"\b{}", "{x}"])
def test_invalid_markup(text):
    with pytest.raises(ValueError):
        parse_markup(text)


def test_docx_add_replace_and_format(tmp_path):
    source = tmp_path / "source.docx"
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("Built ").italic = True
    paragraph.add_run("reports and reports").font.name = "Arial"
    document.add_paragraph("Template")
    document.save(source)
    state = ResumeState(tmp_path / "state")
    state.parse(source)
    output = state.render(tmp_path / "edited.docx", operations=[
        {"op": "format", "id": "p1", "match": "reports", "occurrence": 2,
         "text": r"\b{\i{reports}}"},
        {"op": "add", "id": "p2", "text": r"Saved \b{40%}"},
        {"op": "replace", "id": "p2", "text": r"Using \i{Python}"},
    ])
    paragraphs = Document(output).paragraphs
    assert [p.text for p in paragraphs] == ["Built reports and reports", "Using Python", "Saved 40%"]
    assert paragraphs[0].runs[0].italic is True
    marked = paragraphs[0].runs[-1]
    assert marked.text == "reports" and marked.bold and marked.italic
    assert marked.font.name == "Arial"
    assert paragraphs[1].runs[-1].italic is True
    assert paragraphs[2].runs[-1].bold is True
    for operation in [
        {"op": "format", "id": "p1", "match": "reports", "text": r"\b{reports}"},
        {"op": "format", "id": "p1", "match": "Built", "text": r"\b{Changed}"},
        {"op": "replace", "id": "p2", "text": r"\i{bad"},
    ]:
        with pytest.raises(ValueError):
            state.render(tmp_path / "invalid.docx", operations=[operation])
        assert not (tmp_path / "invalid.docx").exists()


@pytest.mark.parametrize("action", ["format", "replace", "add"])
def test_pdf_inline_styles(tmp_path, action):
    source = tmp_path / "source.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 100), "Built reports using Python")
        document.save(source)
    state = ResumeState(tmp_path / "state")
    view = state.parse(source)
    target = view["sections"][0]["items"][0]["id"]
    operation = {"op": action, "id": target,
                 "text": r"Built \b{reports} using \i{\b{Python}}"}
    output = state.render(tmp_path / "edited.pdf", operations=[operation])
    with pymupdf.open(output) as document:
        spans = [span for block in document[0].get_text("dict")["blocks"]
                 for line in block["lines"] for span in line["spans"]]
        assert any("reports" in s["text"] and s["flags"] & 16 for s in spans)
        assert any("Python" in s["text"] and s["flags"] & 16 and s["flags"] & 2 for s in spans)
        assert "\\b{" not in document[0].get_text()


def test_pdf_format_preserves_existing_styles(tmp_path):
    source = tmp_path / "source.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 100), "Built ", fontname="heit")
        page.insert_text((100, 100), "reports", color=(1, 0, 0))
        document.save(source)
    state = ResumeState(tmp_path / "state")
    view = state.parse(source)
    target = view["sections"][0]["items"][0]["id"]
    output = state.render(tmp_path / "edited.pdf", operations=[
        {"op": "format", "id": target, "match": "reports", "text": r"\b{reports}"},
    ])
    with pymupdf.open(output) as document:
        spans = [span for block in document[0].get_text("dict")["blocks"]
                 for line in block["lines"] for span in line["spans"]]
        assert any("Built" in s["text"] and s["flags"] & 2 for s in spans)
        assert any("reports" in s["text"] and s["color"] == 0xFF0000 and s["flags"] & 16 for s in spans)


def test_cli_format_across_docx_runs(tmp_path, monkeypatch):
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("Built rep").italic = True
    paragraph.add_run("orts today")
    source = tmp_path / "source.docx"
    document.save(source)
    state_dir = tmp_path / "state"
    ResumeState(state_dir).parse(source)
    output = tmp_path / "out.docx"
    monkeypatch.setattr("sys.argv", ["openresume", "--state-dir", str(state_dir),
        "edit", "format", "p1", "--match", "reports", "--text", r"\b{reports}",
        "--output", str(output)])
    main()
    runs = Document(output).paragraphs[0].runs
    assert "".join(run.text for run in runs) == "Built reports today"
    assert [(run.text, run.bold, run.italic) for run in runs] == [
        ("Built ", None, True), ("rep", True, True),
        ("orts", True, None), (" today", None, None),
    ]


def test_pdf_styled_overflow_and_neighbor(tmp_path):
    source = tmp_path / "source.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 100), "Reports")
        page.insert_text((200, 100), "Neighbor")
        document.save(source)
    state = ResumeState(tmp_path / "state")
    view = state.parse(source)
    target = view["sections"][0]["items"][0]["id"]
    output = state.render(tmp_path / "out.pdf", operations=[
        {"op": "replace", "id": target, "text": r"\b{New reports}"},
    ])
    with pymupdf.open(output) as document:
        assert "Neighbor" in document[0].get_text()
    with pytest.raises(ValueError, match="does not fit"):
        state.render(tmp_path / "overflow.pdf", operations=[
            {"op": "replace", "id": target, "text": "\\b{" + "Very long " * 100 + "}"},
        ])
    assert not (tmp_path / "overflow.pdf").exists()
