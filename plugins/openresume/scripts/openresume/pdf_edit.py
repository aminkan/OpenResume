"""Apply text edits to fixed positions in PDF pages."""

import re

import pymupdf

from .markup import parse_markup, format_range

BULLET_PREFIX = re.compile(r"^(?:[•●◦▪▫‣⁃∙·]\s*|[-–—*]\s+|\d+[.)]\s+)")


def _is_background(drawing, bounds):
    return (drawing["type"] == "f" and drawing["fill_opacity"] == 1
            and drawing["rect"].contains(bounds)
            and len(drawing["items"]) == 1 and drawing["items"][0][0] == "re")


def _source_marker(page, line):
    """Locate a marker's original glyphs, including subset-font glyphs."""
    raw = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_TEXT)
    for block in raw["blocks"]:
        for candidate in block.get("lines", []):
            if (tuple(candidate["bbox"]) != tuple(line["bbox"])
                    and list(candidate["bbox"]) not in line.get("source_bboxes", [])):
                continue
            chars = [char for span in candidate["spans"] for char in span["chars"]]
            text = "".join(char["c"] for char in chars)
            left = len(text) - len(text.lstrip())
            prefix = BULLET_PREFIX.match(text[left:])
            if prefix is None:
                return None
            marker = prefix[0].rstrip()
            selected = chars[left:left + len(marker)]
            clip = pymupdf.Rect(selected[0]["bbox"])
            for char in selected[1:]:
                clip |= pymupdf.Rect(char["bbox"])
            body = left + prefix.end()
            advance = (chars[body]["origin"][0] - selected[0]["origin"][0]
                       if body < len(chars) else clip.width)
            if line.get("source_bboxes"):
                advance = line["spans"][1]["origin"][0] - selected[0]["origin"][0]
            return {"clip": clip, "origin": pymupdf.Point(selected[0]["origin"]),
                    "text": marker, "advance": advance, "page": page.number + 1}
    return None


def _copy_marker(page, source, number, marker, origin):
    delta = pymupdf.Point(origin) - marker["origin"]
    clip = marker["clip"]
    # Remove everything outside the glyph before embedding. A display-only
    # clipping mask leaves invisible source text in extraction and bbox logs.
    with pymupdf.open() as glyph:
        glyph.insert_pdf(source, from_page=number - 1, to_page=number - 1, links=False)
        glyph[0].clip_to_rect(clip)
        glyph[0].add_redact_annot(clip, fill=False, cross_out=False)
        glyph[0].apply_redactions(images=1, graphics=2, text=pymupdf.PDF_REDACT_TEXT_NONE)
        page.show_pdf_page(clip + (delta.x, delta.y, delta.x, delta.y),
                           glyph, 0, clip=clip, keep_proportion=False)


def pdf_style(lines):
    spans = [span for line in lines for span in line["spans"] if span["text"].strip()]
    if not spans or any(tuple(line["dir"]) != (1, 0) for line in lines):
        return None
    # Use the predominant text style, ignoring short markers and inline accents.
    first = max(spans, key=lambda span: len(span["text"].strip()))
    flags = first["flags"]
    name = first["font"].lower()
    if "times" in name:
        fonts = ("tiro", "tibo", "tiit", "tibi")
    elif "helvetica" in name or "arial" in name:
        fonts = ("helv", "hebo", "heit", "hebi")
    elif "courier" in name:
        fonts = ("cour", "cobo", "coit", "cobi")
    elif flags & 8:
        fonts = ("cour", "cobo", "coit", "cobi")
    elif flags & 4:
        fonts = ("tiro", "tibo", "tiit", "tibi")
    else:
        fonts = ("helv", "hebo", "heit", "hebi")
    font = fonts[bool(flags & 16) + 2 * bool(flags & 2)]
    return font, first["size"], pymupdf.sRGB_to_pdf(first["color"])


def _variant(font, bold, italic):
    family = next(group for group in (
        ("tiro", "tibo", "tiit", "tibi"),
        ("helv", "hebo", "heit", "hebi"),
        ("cour", "cobo", "coit", "cobi"),
    ) if font in group)
    index = family.index(font)
    return family[(bool(index & 1) if bold is None else bold)
                  + 2 * (bool(index & 2) if italic is None else italic)]


class StyledText:
    def __init__(self, characters, styles):
        self.marker = None
        resolved = []
        for (character, bold, italic), (font, size, color) in zip(characters, styles, strict=True):
            resolved.append((character, (_variant(font, bold, italic), size, color)))
        # Match the existing PDF word wrapping's whitespace normalization.
        raw = "".join(character for character, _ in resolved)
        self.characters = []
        for word in re.finditer(r"\S+", raw):
            if self.characters:
                self.characters.append((" ", resolved[word.start() - 1][1]))
            self.characters.extend(resolved[word.start():word.end()])
        self.text = "".join(character for character, _ in self.characters)
        self.fonts = {font: pymupdf.Font(fontname=font)
                      for font in {style[0] for _, style in self.characters}}
        self.advances = [0]
        for character, style in self.characters:
            if not self.fonts[style[0]].has_glyph(ord(character), fallback=False):
                raise ValueError("The PDF replacement contains characters unsupported by the font.")
            self.advances.append(self.advances[-1] + self.fonts[style[0]].text_length(character, fontsize=style[1]))

    def width(self, start, end):
        return self.advances[end] - self.advances[start]

    def preserve_marker(self, marker):
        if marker is None or not self.text.startswith(marker["text"] + " "):
            return
        self.marker = marker
        prefix_end = len(marker["text"]) + 1
        delta = marker["advance"] - self.advances[prefix_end]
        for index in range(prefix_end, len(self.advances)):
            self.advances[index] += delta

    def draw(self, page, origin, start, end, source, number):
        index = start
        point = pymupdf.Point(origin)
        if start == 0 and self.marker is not None:
            _copy_marker(page, source, self.marker.get("page", number), self.marker, point)
            index = len(self.marker["text"]) + 1
            point.x += self.width(0, index)
        while index < end:
            style = self.characters[index][1]
            stop = index + 1
            while stop < end and self.characters[stop][1] == style:
                stop += 1
            text = "".join(character for character, _ in self.characters[index:stop])
            font, size, color = style
            # Built-in simple-font insertion silently maps U+2022 to U+00B7.
            # Embed the font with a Unicode map instead.
            name = "OpenResume_" + font
            page.insert_font(fontname=name, fontbuffer=self.fonts[font].buffer)
            page.insert_text(point, text, fontname=name, fontsize=size, color=color)
            point.x += self.width(index, stop)
            index = stop


def _styled_edit(operation, lines, base):
    if operation["op"] == "format":
        original = []
        styles = []
        for line in lines:
            line_chars = []
            line_styles = []
            for span in line["spans"]:
                style = pdf_style([{**line, "spans": [span]}])
                line_chars.extend(span["text"])
                line_styles.extend([style] * len(span["text"]))
            raw = "".join(line_chars)
            left = len(raw) - len(raw.lstrip())
            right = len(raw.rstrip())
            if original:
                original.append(" ")
                styles.append(base)
            original.extend(line_chars[left:right])
            styles.extend(line_styles[left:right])
        raw = "".join(original)
        prefix = BULLET_PREFIX.match(raw)
        skip = prefix.end() if prefix else 0
        start, marked = format_range(raw[skip:], operation)
        characters = [(character, None, None) for character in original]
        characters[start + skip:start + skip + len(marked)] = marked
        return StyledText(characters, styles)
    characters = parse_markup(operation["text"])
    original = "".join(span["text"] for span in lines[0]["spans"]).lstrip()
    prefix = BULLET_PREFIX.match(original)
    if operation.get("paragraph"):
        prefix = None
    plain = "".join(character for character, *_ in characters)
    if prefix and not plain.lstrip().startswith(prefix[0].strip()):
        characters = [(character, None, None) for character in prefix[0].strip() + " "] + characters
    return StyledText(characters, [base] * len(characters))


class PdfLayoutError(ValueError):
    """All independently failing operations; no output has been written."""

    def __init__(self, failures):
        self.failures = failures
        super().__init__("PDF layout preflight failed:\n" + "\n".join(
            f"operation {f['operation_index']} ({f['action']} {f['id']}): {f['reason']}"
            for f in failures))


def _clear_right(page, rectangle):
    right = max(rectangle.x1, page.rect.x1 - max(36, rectangle.x0))
    obstacles = [pymupdf.Rect(line['bbox'])
                 for block in page.get_text('dict')['blocks'] if 'lines' in block
                 for line in block['lines']]
    obstacles.extend(d['rect'] for d in page.get_drawings() if not _is_background(d, page.rect))
    obstacles.extend(pymupdf.Rect(i['bbox']) for i in page.get_image_info())
    for obstacle in obstacles:
        if obstacle.x0 >= rectangle.x1 and obstacle.y0 < rectangle.y1 and obstacle.y1 > rectangle.y0:
            right = min(right, obstacle.x0 - 1)
    return right


def edit_pdf_text(data: bytes, operations: list[dict], *, layout="fixed") -> bytes:
    if layout == "reflow":
        from .pdf_reflow import reflow_pdf
        return reflow_pdf(data, operations)
    if layout != "fixed":
        raise ValueError("PDF layout must be fixed or reflow.")
    failures = []
    for index, operation in enumerate(operations, 1):
        try:
            _edit_pdf_text(data, [operation])
        except ValueError as error:
            failures.append(_failure(data, operation, index, error))
    if failures:
        raise PdfLayoutError(failures)
    try:
        return _edit_pdf_text(data, operations)
    except ValueError as error:
        raise PdfLayoutError([{
            "operation_index": list(range(1, len(operations) + 1)),
            "id": [op.get("source_id", "unknown") for op in operations],
            "action": "batch", "reason": str(error),
        }]) from error


def _failure(data, operation, index, error):
    with pymupdf.open(stream=data, filetype="pdf") as document:
        page = document[operation["page"] - 1]
        lines = operation.get("template_lines", operation["lines"])
        widths = [_clear_right(page, pymupdf.Rect(line["bbox"])) - line["spans"][0]["origin"][0]
                  for line in lines]
    return {"operation_index": operation.get("operation_index", index), "id": operation.get("source_id", "unknown"),
            "action": operation["op"], "reason": str(error),
            "available_widths": widths, "line_count": len(lines),
            "remaining_text": str(error).partition("Remaining text: ")[2] or None,
            "requires_movement": "clear space" in str(error) or "does not fit" in str(error)}


def _edit_pdf_text(data: bytes, operations: list[dict]) -> bytes:
    with (pymupdf.open(stream=data, filetype="pdf") as document,
          pymupdf.open(stream=data, filetype="pdf") as original_document):
        plans = {}
        gaps = {}
        seen = set()
        insertion_bottoms = {}
        for operation in operations:
            number, anchor, action = (operation[key] for key in ("page", "lines", "op"))
            text = operation.get("text", "")
            if action != "remove" and (
                not isinstance(text, str) or not text.strip() or any(c in text for c in "\r\n\t")
            ):
                raise ValueError("PDF edit text must be nonempty and contain no line breaks or tabs.")
            page = document[number - 1]
            if page.rotation:
                raise ValueError("PDF edits on rotated pages are unsupported.")
            lines = operation.get("template_lines", anchor)
            style = pdf_style(lines)
            if style is None:
                raise ValueError("Unsupported PDF text direction or style.")
            font, size, color = style
            rich = None if action == "remove" else _styled_edit(operation, lines, (font, size, color))
            if rich is not None:
                marker_page = original_document[operation.get("template_page", number) - 1]
                rich.preserve_marker(_source_marker(marker_page, lines[0]))
            text = rich.text if rich is not None else ""
            words = text.split()
            word_starts = [match.start() for match in re.finditer(r"\S+", text)]
            index = 0
            slots = []
            offset = 0
            if action == "add":
                anchor_key = (number, tuple(anchor[-1]["bbox"]))
                bottom = insertion_bottoms.get(anchor_key, anchor[-1]["bbox"][3])
                offset = bottom + 1 - lines[0]["bbox"][1]
            for line in lines:
                rectangle = pymupdf.Rect(line["bbox"])
                origin = pymupdf.Point(line["spans"][0]["origin"])
                key = (number, tuple(rectangle))
                if action != "add":
                    if key in seen:
                        raise ValueError("A PDF text line can only be edited once.")
                    seen.add(key)
                replacement = ""
                start = word_starts[index] if index < len(words) else len(text)
                destination = rectangle + (0, offset, 0, offset)
                # Templates supply appearance, not the destination's text width.
                right = _clear_right(page, destination)
                while index < len(words):
                    candidate = f"{replacement} {words[index]}" if replacement else words[index]
                    if rich.width(start, start + len(candidate)) > right - origin.x:
                        break
                    replacement = candidate
                    index += 1
                # Use a thin band inside the glyph boxes to avoid touching adjacent lines.
                middle = (rectangle.y0 + rectangle.y1) / 2
                band = pymupdf.Rect(rectangle.x0, middle - 0.1, rectangle.x1, middle + 0.1)
                if action == "add":
                    rectangle.x1 = max(rectangle.x1, origin.x + rich.width(start, start + len(replacement)))
                    rectangle += (0, offset, 0, offset)
                    origin += (0, offset)
                    if replacement:
                        slots.append((rectangle, None, origin, replacement))
                    continue
                slots.append((rectangle, band, origin, replacement))
            if index < len(words):
                remaining = " ".join(words[index:])
                raise ValueError(f"The PDF text does not fit in the template lines. Remaining text: {remaining}")
            if action == "add":
                insertion_bottoms[anchor_key] = max(slot[0].y1 for slot in slots)
            if action == "replace":
                gap = _trailing_gap(slots)
                if gap is not None:
                    gaps.setdefault(number, []).append(gap)
            plans.setdefault(number, []).append((slots, rich, size, color))

        for number, replacements in plans.items():
            page = document[number - 1]
            if any(annotation.type[0] == pymupdf.PDF_ANNOT_REDACT for annotation in page.annots()):
                raise ValueError("The PDF page already contains pending redactions.")
            layout = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)
            boxes = [
                pymupdf.Rect(line["bbox"])
                for block in layout["blocks"] for line in block["lines"]
            ]
            additions = []
            obstacles = []
            if any(band is None for slots, *_ in replacements for _, band, _, _ in slots):
                obstacles = [drawing["rect"] for drawing in page.get_drawings()
                             if not _is_background(drawing, page.rect)]
                obstacles.extend(pymupdf.Rect(image["bbox"]) for image in page.get_image_info())
                obstacles.extend(annotation.rect for annotation in page.annots())
                obstacles.extend(link["from"] for link in page.get_links())
            for slots, _, _, _ in replacements:
                for rectangle, band, _, _ in slots:
                    if band is None:
                        if not page.rect.contains(rectangle) or any(
                            box.intersects(rectangle) for box in boxes + obstacles + additions
                        ):
                            raise ValueError("The PDF addition needs clear space after the target.")
                        continue
                    covered = [box for box in boxes if rectangle.contains(box)]
                    if not covered or any(box not in covered and box.intersects(band) for box in boxes):
                        raise ValueError("The PDF target overlaps other text.")
                    page.add_redact_annot(band, fill=False, cross_out=False)
                additions.extend(rectangle for rectangle, band, _, _ in slots if band is None)
            links = page.get_links()
            page.apply_redactions(images=0, graphics=0)
            # Redaction may drop intersecting links even for wording-only edits.
            surviving = {(link['kind'], tuple(link['from']), link.get('uri')) for link in page.get_links()}
            for link in links:
                if (link['kind'], tuple(link['from']), link.get('uri')) not in surviving:
                    page.insert_link({k: v for k, v in link.items() if k not in {'xref', 'id'}})
            for slots, rich, size, color in replacements:
                cursor = 0
                for _, _, origin, replacement in slots:
                    if replacement:
                        start = rich.text.find(replacement, cursor)
                        rich.draw(page, origin, start, start + len(replacement), original_document, number)
                        cursor = start + len(replacement)
            if number in gaps:
                _close_gaps(page, gaps[number])
        return document.tobytes(garbage=4)


def _trailing_gap(slots):
    """Find the unused trailing line positions of a shorter replacement."""
    occupied = [index for index, slot in enumerate(slots) if slot[3]]
    if not occupied or occupied[-1] == len(slots) - 1:
        return None
    last_used = occupied[-1]
    if occupied != list(range(last_used + 1)):
        return None
    first_blank = slots[last_used + 1]
    cut = (first_blank[0].y0 + first_blank[0].y1) / 2
    return cut, slots[-1][2].y - slots[last_used][2].y


def _close_gaps(page, gaps):
    """Move intact horizontal bands up while retaining the original PDF objects."""
    if page.first_annot or page.first_widget or page.cropbox_position != pymupdf.Point(0, 0):
        return
    links = page.get_links()
    # Internal destinations may refer to content whose position would change.
    if any(link["kind"] != pymupdf.LINK_URI for link in links):
        return

    bounds = page.rect
    # A page-wide solid background does not occupy the blank lines. Retain it
    # in the copied bands, but do not let its bounding box prevent compaction.
    backgrounds = {
        drawing["seqno"] for drawing in page.get_drawings()
        if _is_background(drawing, bounds)
    }
    objects = [pymupdf.Rect(box) for index, (_, box) in enumerate(page.get_bboxlog())
               if index not in backgrounds]
    objects.extend(pymupdf.Rect(link["from"]) for link in links)
    bands = []
    top = bounds.y0
    shift = 0
    applied = []
    for cut, height in sorted(gaps):
        if not top < cut < bounds.y1 or height <= 0:
            continue
        if any(box.y0 <= cut <= box.y1 for box in objects):
            continue
        above = [box.y1 for box in objects if box.y1 < cut]
        below = [box.y0 for box in objects if box.y0 > cut]
        if not above or not below or min(below) - max(above) + 0.5 < height:
            continue
        # Exclude the actual empty strip so opaque page backgrounds cannot
        # paint over the preceding line when the lower band moves upward.
        strip_start = max(max(above), (max(above) + min(below) - height) / 2)
        strip_end = strip_start + height
        bands.append((pymupdf.Rect(bounds.x0, top, bounds.x1, strip_start), shift))
        top = strip_end
        shift += height
        applied.append((cut, height))
    if not bands:
        return
    bands.append((pymupdf.Rect(bounds.x0, top, bounds.x1, bounds.y1), shift))

    document = page.parent
    with pymupdf.open() as compacted:
        target = compacted.new_page(width=bounds.width, height=bounds.height)
        for clip, offset in bands:
            # A fresh source avoids stale font/XObject graft maps after clipping.
            with pymupdf.open() as source:
                source.insert_pdf(document, from_page=page.number, to_page=page.number, links=False)
                source[0].clip_to_rect(clip)
                if source[0].get_bboxlog():
                    target.show_pdf_page(clip + (0, -offset, 0, -offset), source, 0, clip=clip)
        # Keep the page object and its links. Only replace its visible contents.
        contents = document.get_new_xref()
        document.update_object(contents, "<<>>")
        document.update_stream(contents, b"")
        page.set_contents(contents)
        page.show_pdf_page(bounds, compacted)
    # URI links remain interactive and follow the moved content.
    for link in links:
        offset = sum(height for cut, height in applied if link["from"].y0 >= cut)
        if offset:
            page.update_link({**link, "from": link["from"] + (0, -offset, 0, -offset)})
