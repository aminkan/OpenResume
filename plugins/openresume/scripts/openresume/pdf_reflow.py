"""Explicit single-column PDF reflow, retaining untouched source glyphs.

Unchanged horizontal bands are copied as PDF objects. Edited paragraphs are
wrapped in standard fonts. Unsupported features fail before output is written.
"""

import re

import pymupdf

from .pdf_edit import (_is_background, _source_marker, _styled_edit, pdf_style,
                       BULLET_PREFIX, PdfLayoutError, _failure)


def _key(line):
    return tuple(line["bbox"])


def _wrap(rich, x, right, indent):
    words = list(re.finditer(r"\S+", rich.text))
    rows = []
    index = 0
    while index < len(words):
        start = words[index].start()
        stop = index
        left = x if not rows else x + indent
        while stop < len(words) and rich.width(start, words[stop].end()) <= right - left:
            stop += 1
        if stop == index:
            raise ValueError(f"A word exceeds the reflow column width: {words[index][0]}")
        rows.append((start, words[stop - 1].end(), left))
        index = stop
    if not rows:
        raise ValueError("PDF edit text must be nonempty.")
    return rows


def _edited_unit(page, operation):
    lines = operation.get("template_lines", operation["lines"])
    style = pdf_style(lines)
    if style is None:
        raise ValueError("Unsupported PDF text direction or style.")
    text = operation.get("text")
    if not isinstance(text, str) or not text.strip() or any(c in text for c in "\r\n\t"):
        raise ValueError("PDF edit text must be nonempty and contain no line breaks or tabs.")
    rich = _styled_edit(operation, lines, style)
    marker_page = page.parent[operation.get("template_page", page.number + 1) - 1]
    rich.preserve_marker(_source_marker(marker_page, lines[0]))
    x = lines[0]["spans"][0]["origin"][0]
    # A style template never limits insertion width to its printed text extent.
    right = page.rect.x1 - max(36, x)
    prefix = BULLET_PREFIX.match(rich.text)
    indent = rich.width(0, prefix.end()) if prefix else 0
    rows = _wrap(rich, x, right, indent)
    ascent = max(rich.fonts[s[0]].ascender * s[1] for _, s in rich.characters)
    descent = max(-rich.fonts[s[0]].descender * s[1] for _, s in rich.characters)
    height = ascent + descent
    step = max(height + 1, style[1] * 1.25)
    return {"rich": rich, "rows": rows, "ascent": ascent, "row_height": height,
            "step": step, "height": height + (len(rows) - 1) * step,
            "heading": operation.get("keep_with_next", False) or rich.text.rstrip(":").casefold() in {
                "summary", "professional summary", "profile", "experience", "education",
                "skills", "technical skills", "projects", "publications", "certifications",
            }}


def _source_units(page):
    if page.rotation or page.cropbox_position != pymupdf.Point(0, 0):
        raise ValueError("Reflow does not support rotated or offset-cropped pages.")
    if page.first_annot or page.first_widget:
        raise ValueError("Reflow cannot preserve annotations or form fields; use fixed layout or an editable DOCX.")
    if any(link["kind"] != pymupdf.LINK_URI for link in page.get_links()):
        raise ValueError("Reflow supports URI links only; internal or other link destinations need fixed layout.")
    if page.get_image_info():
        raise ValueError("Reflow does not support images; use fixed layout or an editable DOCX.")
    objects = []
    for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]:
        for line in block["lines"]:
            if tuple(line["dir"]) != (1, 0):
                raise ValueError("Reflow requires horizontal text.")
            objects.append({"rect": pymupdf.Rect(line["bbox"]), "lines": [line]})
    backgrounds = []
    for drawing in page.get_drawings():
        if _is_background(drawing, page.rect):
            backgrounds.append(drawing["fill"])
        else:
            rect = pymupdf.Rect(drawing["rect"])
            if rect.height > 2 or drawing.get("type") != "s":
                raise ValueError("Reflow supports horizontal rules and solid backgrounds only.")
            rect.y0 -= 1
            rect.y1 += 1
            objects.append({"rect": rect, "lines": []})
    # Include link extents so no annotation is cut across copied bands.
    for link in page.get_links():
        objects.append({"rect": pymupdf.Rect(link["from"]), "lines": []})
    units = []
    for obj in sorted(objects, key=lambda o: o["rect"].y0):
        if units and obj["rect"].y0 < units[-1]["rect"].y1:
            units[-1]["rect"] |= obj["rect"]
            units[-1]["lines"].extend(obj["lines"])
        else:
            units.append(obj)
    # Side-by-side prose columns need a reading-order model rather than bands.
    parallel = 0
    for unit in units:
        lines = sorted(unit["lines"], key=lambda line: line["bbox"][0])
        if len(lines) > 1 and all(len("".join(s["text"] for s in line["spans"])) > 35 for line in lines):
            parallel += 1
    if parallel >= 3:
        raise ValueError("Reflow does not support multiple prose columns; use fixed layout or an editable DOCX.")
    return units, backgrounds


def _page_plan(page, operations):
    units, backgrounds = _source_units(page)
    replacements = {}
    additions = {}
    failures = []
    for index, operation in operations:
        try:
            keys = {tuple(box) for line in operation["lines"]
                    for box in line.get("source_bboxes", [line["bbox"]])}
            indices = [i for i, u in enumerate(units) if any(_key(line) in keys for line in u["lines"])]
            if not indices:
                raise ValueError("Unknown PDF target geometry.")
            if operation["op"] == "add":
                additions.setdefault(max(indices), []).append(_edited_unit(page, operation))
                continue
            if any(i in replacements for i in indices):
                raise ValueError("A PDF text line can only be edited once.")
            if any(_key(line) not in keys for i in indices for line in units[i]["lines"]):
                raise ValueError("Reflow target shares a horizontal band with other text; target the complete row or use fixed layout.")
            replacement = None if operation["op"] == "remove" else _edited_unit(page, operation)
            if replacement is not None:
                replacement["links"] = [link for link in page.get_links()
                                        if any(units[i]["rect"].contains(link["from"]) for i in indices)]
                if len(replacement["links"]) > 1:
                    raise ValueError("Reflow cannot map multiple links inside a replaced paragraph; use fixed layout.")
            replacements[min(indices)] = replacement
            for i in indices[1:]:
                replacements[i] = None
        except ValueError as error:
            failures.append(_failure(page.parent.tobytes(), operation, index, error))
    if failures:
        raise PdfLayoutError(failures)
    plan = []
    previous_bottom = units[0]["rect"].y0 if units else 36
    for i, unit in enumerate(units):
        gap = max(0, unit["rect"].y0 - previous_bottom)
        previous_bottom = unit["rect"].y1
        content = replacements.get(i, unit)
        if content is not None:
            content = {**content, "gap": gap}
            if "rich" not in content:
                content["height"] = content["rect"].height
                texts = ["".join(s["text"] for s in line["spans"]).strip().rstrip(":").casefold()
                         for line in content["lines"]]
                content["heading"] = any(t in {"experience", "education", "skills", "projects",
                                                "publications", "summary", "professional summary"} for t in texts)
            plan.append(content)
        for addition in additions.get(i, []):
            plan.append({**addition, "gap": 3})
    return plan, backgrounds


def reflow_pdf(data, operations):
    with pymupdf.open(stream=data, filetype="pdf") as source, pymupdf.open() as output:
        plans = []
        failures = []
        for page in source:
            page_ops = [(i, op) for i, op in enumerate(operations, 1) if op["page"] == page.number + 1]
            try:
                plans.append(_page_plan(page, page_ops))
            except PdfLayoutError as error:
                failures.extend(error.failures)
            except ValueError as error:
                failures.append({"operation_index": [i for i, _ in page_ops], "id": f"page-{page.number + 1}",
                                 "action": "reflow", "reason": str(error)})
        if failures:
            raise PdfLayoutError(failures)
        for page, (plan, backgrounds) in zip(source, plans):
            top = max(18, min(36, plan[0]["rect"].y0 if plan and "rect" in plan[0] else 36))
            bottom = page.rect.height - 36

            def new_page():
                target = output.new_page(width=page.rect.width, height=page.rect.height)
                for color in backgrounds:
                    target.draw_rect(target.rect, color=None, fill=color)
                return target

            target = new_page()
            y = top
            for index, unit in enumerate(plan):
                gap = unit["gap"] if y > top else 0
                keep = 0
                if unit["heading"]:
                    for following in plan[index + 1:]:
                        keep += following["gap"] + following.get("row_height", following["height"])
                        if "rich" in following or following.get("lines"):
                            break
                if y > top and y + gap + unit["height"] + keep > bottom:
                    target = new_page()
                    y, gap = top, 0
                y += gap
                if "rich" in unit:
                    rich = unit["rich"]
                    # Paragraphs larger than a page split only at wrapped lines.
                    for row_index, (start, end, x) in enumerate(unit["rows"]):
                        if y + unit["row_height"] > bottom:
                            target = new_page()
                            y = top
                        rich.draw(target, (x, y + unit["ascent"]), start, end, source, page.number + 1)
                        if row_index == 0:
                            for link in unit.get("links", []):
                                target.insert_link({"kind": pymupdf.LINK_URI, "uri": link["uri"],
                                                    "from": pymupdf.Rect(x, y, x + rich.width(start, end),
                                                                         y + unit["row_height"])})
                        y += unit["step"] if row_index + 1 < len(unit["rows"]) else unit["row_height"]
                else:
                    rect = unit["rect"]
                    if unit["height"] > bottom - top:
                        raise ValueError("An indivisible source band exceeds a page; use an editable DOCX.")
                    clip = pymupdf.Rect(0, rect.y0, page.rect.width, rect.y1)
                    destination = clip + (0, y - rect.y0, 0, y - rect.y0)
                    # Real clipping removes hidden text outside the selected band.
                    with pymupdf.open() as band:
                        band.insert_pdf(source, from_page=page.number, to_page=page.number, links=False)
                        band[0].clip_to_rect(clip)
                        if band[0].get_bboxlog():
                            target.show_pdf_page(destination, band, 0, clip=clip, keep_proportion=False)
                    for link in page.get_links():
                        if rect.y0 <= link["from"].y0 and link["from"].y1 <= rect.y1:
                            target.insert_link({"kind": pymupdf.LINK_URI, "uri": link["uri"],
                                                "from": link["from"] + (0, y - rect.y0, 0, y - rect.y0)})
                    y += unit["height"]
        from collections import Counter
        expected = Counter()
        for page, (plan, _) in zip(source, plans):
            for unit in plan:
                links = unit.get("links", []) if "rich" in unit else [
                    link for link in page.get_links() if unit["rect"].contains(link["from"])]
                expected.update(link["uri"] for link in links)
        # Reload to resolve freshly inserted link annotations before validation.
        with pymupdf.open(stream=output.tobytes(), filetype="pdf") as checked:
            actual = Counter(link["uri"] for page in checked for link in page.get_links())
            if actual != expected:
                raise ValueError("Reflow link validation failed; no output was written.")
            for page in checked:
                bounds = page.rect + (-0.5, -0.5, 0.5, 0.5)
                if any(not bounds.contains(pymupdf.Rect(word[:4])) for word in page.get_text("words")):
                    raise ValueError("Reflow text exceeds page bounds; no output was written.")
        output.set_metadata(source.metadata)
        return output.tobytes(garbage=4)
