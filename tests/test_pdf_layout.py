import pymupdf
import pytest

from openresume.state import ResumeState


def make_bullets(path, background=False):
    with pymupdf.open() as document:
        page = document.new_page()
        if background:
            page.draw_rect(page.rect, fill=(1, 1, 1), color=None)
        font = pymupdf.Font("tiro")
        page.insert_font(fontname="Original", fontbuffer=font.buffer)
        page.insert_text((72, 100), "\u2022 Built and maintained automated reporting tools", fontname="Original", fontsize=10)
        page.insert_text((78, 112), "for internal analytics and operations teams", fontname="Original", fontsize=10)
        page.insert_text((72, 124), "\u2022 Next bullet", fontname="Original", fontsize=10)
        page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(78, 115, 130, 126),
                          "uri": "https://example.com"})
        document.save(path)


@pytest.mark.parametrize("action", ["replace", "add", "format"])
def test_bullet_glyph_survives_with_original_font(tmp_path, action):
    source = tmp_path / "source.pdf"
    make_bullets(source, background=True)
    state = ResumeState(tmp_path / "state")
    view = state.parse(source)
    items = view["sections"][0]["items"]
    target = items[0] if action != "add" else items[-1]
    operation = {"op": action, "id": target["id"], "text": r"\b{Short report}"}
    if action == "format":
        operation.update(match="Built", text=r"\b{Built}")
    output = state.render(tmp_path / "out.pdf", operations=[operation])
    with pymupdf.open(output) as document:
        spans = [s for b in document[0].get_text("dict")["blocks"]
                 for line in b.get("lines", []) for s in line["spans"]]
        text = document[0].get_text()
        assert text.count("\u2022") == (3 if action == "add" else 2)
        assert "\u00b7" not in text
        assert all(s["font"] == "NimbusRoman-Regular" for s in spans if "\u2022" in s["text"])


@pytest.mark.parametrize("background", [False, True])
def test_shorter_styled_bullet_compacts_and_moves_links(tmp_path, background):
    source = tmp_path / "source.pdf"
    make_bullets(source, background=background)
    state = ResumeState(tmp_path / "state")
    view = state.parse(source)
    output = state.render(tmp_path / "out.pdf", operations=[
        {"op": "replace", "id": view["sections"][0]["items"][0]["id"], "text": r"\b{Short report}"},
    ])
    with pymupdf.open(output) as document:
        words = document[0].get_text("words")
        short = next(w for w in words if w[4] == "Short")
        following = next(w for w in words if w[4] == "Next")
        assert following[1] - short[1] < 15
        links = document[0].get_links()
        assert len(links) == 1 and links[0]["uri"] == "https://example.com"
        assert links[0]["from"].y0 == pytest.approx(103, abs=0.05)
