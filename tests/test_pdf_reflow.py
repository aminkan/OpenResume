"""Regression cases from the November 2026 PDF diagnostic report."""

from collections import Counter

import pymupdf
import pytest

from openresume.state import ResumeState
from openresume.pdf_edit import PdfLayoutError
from openresume import main
import json


def fixture_resume(tmp_path, *, separate=False, annotation=False):
    source = tmp_path / "source.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=360, height=300)
        page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
        page.insert_text((36, 40), "Candidate")
        page.insert_text((36, 58), "Email | LinkedIn")
        page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(36, 47, 63, 60),
                          "uri": "mailto:candidate@example.com"})
        page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(74, 47, 115, 60),
                          "uri": "https://linkedin.com/in/candidate"})
        page.insert_text((36, 80), "Experience", fontname="hebo")
        if separate:
            page.insert_text((36, 100), "- ")
            page.insert_text((48, 100), "Built reports")
        else:
            page.insert_text((36, 100), "- Built reports")
        page.insert_text((36, 120), "- Kept existing bullet")
        page.insert_text((36, 140), "Skills", fontname="hebo")
        page.insert_text((36, 160), "Python and SQL")
        if annotation:
            page.add_text_annot((320, 180), "Review")
        doc.save(source)
    state = ResumeState(tmp_path / "state")
    return state, state.parse(source)


def payload(view, operations):
    return {"source_sha256": view["source_sha256"], "operations": operations}


def test_plain_replacement_uses_clear_column(tmp_path):
    state, view = fixture_resume(tmp_path)
    bullet = view["sections"][1]["items"][0]
    output = state.render(tmp_path / "longer.pdf", operations=[
        {"op": "replace", "id": bullet["id"], "text": "Built reports for all internal teams"}])
    with pymupdf.open(output) as doc:
        assert "Built reports for all internal teams" in doc[0].get_text()
        assert len(doc[0].get_links()) == 2


def test_preflight_reports_every_failure_and_writes_nothing(tmp_path):
    state, view = fixture_resume(tmp_path)
    ids = [item["id"] for item in view["sections"][1]["items"]]
    operations = [{"op": "replace", "id": i, "text": "Too long " * 80} for i in ids]
    before = set(tmp_path.rglob("*"))
    report = state.preflight(payload(view, operations))
    assert not report["valid"]
    assert [f["id"] for f in report["failures"]] == ids
    assert all(f["available_widths"] and f["line_count"] and f["remaining_text"] for f in report["failures"])
    assert set(tmp_path.rglob("*")) == before
    output = tmp_path / "failed.pdf"
    with pytest.raises(PdfLayoutError) as caught:
        state.render(output, operations=operations)
    assert len(caught.value.failures) == 2
    assert not output.exists()


def test_invalid_ids_are_aggregated(tmp_path):
    state, view = fixture_resume(tmp_path)
    report = state.preflight(payload(view, [
        {"op": "remove", "id": "bad-1"}, {"op": "remove", "id": "bad-2"}]))
    assert [f["id"] for f in report["failures"]] == ["bad-1", "bad-2"]


def test_longer_heading_uses_template_style_and_destination_width(tmp_path):
    state, view = fixture_resume(tmp_path)
    last = view["sections"][-1]["items"][0]
    heading = view["sections"][-1]["heading"]
    output = state.render(tmp_path / "heading.pdf", operations=[
        {"op": "add", "id": last["id"], "template_id": heading["id"], "text": "Professional Summary"}])
    with pymupdf.open(output) as doc:
        span = next(s for b in doc[0].get_text("dict")["blocks"] for l in b["lines"]
                    for s in l["spans"] if s["text"] == "Professional Summary")
        assert span["flags"] & 16


def test_section_insertion_wraps_paginates_and_preserves_links(tmp_path):
    state, view = fixture_resume(tmp_path)
    contact = view["sections"][0]["items"][-1]
    section = view["sections"][1]
    summary = "An experienced engineer building reliable systems. " * 10
    replacement = "Built dependable automated reports for internal teams. " * 15
    operations = [
        {"op": "add_section", "id": contact["id"], "heading": "Professional Summary", "text": summary,
         "heading_template_id": section["heading"]["id"],
         "template_id": section["items"][0]["id"]},
        {"op": "replace", "id": section["items"][0]["id"], "text": replacement},
    ]
    report = state.preflight(payload(view, operations), layout="reflow")
    assert report["valid"] and report["pages"] > 1 and report["links"] == 2
    output = state.render(tmp_path / "reflow.pdf", operations=operations, layout="reflow")
    with pymupdf.open(output) as doc:
        text = " ".join(" ".join(p.get_text() for p in doc).split())
        assert " ".join(summary.split()) in text
        assert " ".join(replacement.split()) in text
        assert "Kept existing bullet" in text and "Python and SQL" in text
        assert text.index("Professional Summary") < text.index("An experienced") < text.index("Experience")
        assert "- An experienced" not in text
        assert Counter(link["uri"] for p in doc for link in p.get_links()) == Counter({
            "mailto:candidate@example.com": 1, "https://linkedin.com/in/candidate": 1})
        for page in doc:
            assert all(page.rect.contains(pymupdf.Rect(word[:4])) for word in page.get_text("words"))
            lines = [line for b in page.get_text("dict")["blocks"] for line in b["lines"]]
            if "Professional Summary" in page.get_text():
                assert "An experienced" in page.get_text()
            # Wrapped bullet continuation aligns after its marker.
            starts = [l["bbox"][0] for l in lines if "dependable" in "".join(s["text"] for s in l["spans"])]
            assert all(x >= 36 for x in starts)


@pytest.mark.parametrize("layout", ["fixed", "reflow"])
def test_separate_bullet_marker_and_body_are_one_editable_item(tmp_path, layout):
    state, view = fixture_resume(tmp_path, separate=True)
    bullet = view["sections"][1]["items"][0]
    assert bullet["kind"] == "bullet" and bullet["text"] == "Built reports"
    assert all(item["text"] for section in view["sections"] for item in section["items"])
    output = state.render(tmp_path / "marker.pdf", operations=[
        {"op": "replace", "id": bullet["id"], "text": "Updated reports"}], layout=layout)
    with pymupdf.open(output) as doc:
        text = " ".join(doc[0].get_text().split())
        assert "Updated reports" in text and "Built reports" not in text
        assert text.count("-") == 2


def test_far_or_different_baseline_marker_is_not_associated(tmp_path):
    source = tmp_path / "unrelated.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((36, 60), "-")
        page.insert_text((150, 60), "Far away")
        page.insert_text((48, 80), "Different row")
        doc.save(source)
    state = ResumeState(tmp_path / "state")
    view = state.parse(source)
    assert len(view["sections"][0]["items"]) == 3


def test_unsupported_features_fail_explicitly(tmp_path):
    state, view = fixture_resume(tmp_path, annotation=True)
    report = state.preflight(payload(view, []), layout="reflow")
    assert not report["valid"] and "annotations" in report["failures"][0]["reason"]


def test_overwrite_message_is_clear(tmp_path):
    state, _ = fixture_resume(tmp_path)
    out = state.render(tmp_path / "copy.pdf")
    before = out.read_bytes()
    with pytest.raises(FileExistsError, match="choose a new path"):
        state.render(out)
    assert out.read_bytes() == before


def test_fixed_expansion_respects_adjacent_column(tmp_path):
    source = tmp_path / "columns.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=360, height=300)
        page.insert_text((36, 60), "Left")
        page.insert_text((105, 60), "Right column")
        doc.save(source)
    state = ResumeState(tmp_path / "state")
    view = state.parse(source)
    left = view["sections"][0]["items"][0]
    report = state.preflight(payload(view, [
        {"op": "replace", "id": left["id"], "text": "A longer replacement would overwrite the right column"}]))
    assert not report["valid"]
    assert report["failures"][0]["available_widths"][0] == pytest.approx(68)


def test_heading_keeps_with_text_across_horizontal_rule(tmp_path):
    source = tmp_path / "heading.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=360, height=220)
        page.insert_text((36, 40), "Candidate")
        page.insert_text((36, 155), "Education", fontname="hebo")
        page.draw_line((36, 159), (324, 159))
        page.insert_text((36, 181), "University degree")
        doc.save(source)
    state = ResumeState(tmp_path / "state")
    state.parse(source)
    output = state.render(tmp_path / "flow.pdf", layout="reflow")
    with pymupdf.open(output) as doc:
        assert len(doc) == 2
        assert "Education" not in doc[0].get_text()
        assert "Education" in doc[1].get_text() and "University degree" in doc[1].get_text()


def test_replacing_linked_text_retains_destination(tmp_path):
    source = tmp_path / "linked.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=360, height=300)
        page.insert_text((36, 60), "Website")
        page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(36, 47, 80, 64),
                          "uri": "https://example.com"})
        doc.save(source)
    state = ResumeState(tmp_path / "state")
    view = state.parse(source)
    operations = [{"op": "replace", "id": view["sections"][0]["items"][0]["id"],
                   "text": "My professional website"}]
    for layout in ("fixed", "reflow"):
        output = state.render(tmp_path / f"{layout}.pdf", operations=operations, layout=layout)
        with pymupdf.open(output) as doc:
            assert doc[0].get_links()[0]["uri"] == "https://example.com"
            assert "My professional website" in doc[0].get_text()


def test_cli_preflight_and_render_use_same_mode(tmp_path, monkeypatch, capsys):
    state, view = fixture_resume(tmp_path)
    changes = tmp_path / "changes.json"
    operations = [{"op": "replace", "id": view["sections"][1]["items"][0]["id"], "text": "Large paragraph " * 30}]
    changes.write_text(json.dumps(payload(view, operations)), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["openresume", "--state-dir", str(state.directory),
                                    "preflight", "--edits", str(changes)])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
    assert not json.loads(capsys.readouterr().out)["valid"]
    monkeypatch.setattr("sys.argv", ["openresume", "--state-dir", str(state.directory),
                                    "preflight", "--edits", str(changes), "--layout", "reflow"])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["valid"] and report["pages"] > 1
    output = tmp_path / "cli.pdf"
    monkeypatch.setattr("sys.argv", ["openresume", "--state-dir", str(state.directory),
                                    "render", str(output), "--edits", str(changes), "--layout", "reflow"])
    main()
    assert json.loads(capsys.readouterr().out)["output"] == str(output)
    with pymupdf.open(output) as doc:
        assert len(doc) == report["pages"]
