# OpenResume

Local commands for parsing a resume and rendering it from saved project state.

## Codex desktop plugin

The installable plugin is in `plugins/openresume/`. That folder contains its
manifest, skill, logo, Python source, and a launcher. The same package works
on Windows, macOS, and Linux where Python 3.11 or newer with `venv` and `pip`
is available and the dependencies provide compatible wheels.

On first use, the launcher installs dependencies into a local environment under
`~/.openresume/runtime/`. That step needs access to the Python package index.
Subsequent runs reuse the environment. Resume sessions live separately under
`~/.openresume/sessions/`.

To share locally, give the recipient the repository marketplace file and the
complete `plugins/openresume/` folder with the same relative paths. They can
register the unpacked marketplace root with `codex plugin marketplace add <folder>`,
then install OpenResume from the Plugins Directory.

## Command-line use

Run the bundled CLI with `python plugins/openresume/scripts/run.py` followed by
the arguments below. If you installed the source with `python -m pip install -e .`,
you can use the `openresume` command directly as shown.

```powershell
openresume parse resume.docx
openresume render output.docx
openresume parse resume.pdf
openresume render output.pdf
```

`parse` prints a compact agent view and saves the full parse to
`.openresume/active.json` alongside a snapshot of the original file.
`openresume view` returns the compact view again. Each successful
parse selects a new active resume.
Run subsequent commands in the same directory, or pass the same
`--state-dir <directory>` before the subcommand. The state contains private
resume data and is ignored by Git in this project. Prior snapshots are retained.

The DOCX agent view groups text under recognized Word heading styles and gives
each paragraph a short ID. The full local parse holds the source locations,
runs, links, and formatting properties. The PDF agent view groups text under
common section headings and uses IDs that point to source pages, blocks, and
lines. Unrecognized text stays in the extracted reading order. Review section
boundaries and reading order for multi-column PDFs.

`edit` applies one operation to the active resume and writes a new file.
Use an ID from `view` and choose an output extension that matches the source.

```powershell
openresume edit replace p3 --text "Built automated reports." --output tailored.docx
openresume edit add p3 --template-id p2 --text "Projects" --output with-projects.docx
openresume edit remove p4 --output shortened.docx
openresume edit replace p3 --text 'Built \b{reports} using \i{Python}.' --output styled.docx
openresume edit format p3 --match 'reports' --text '\b{reports}' --output emphasized.docx
```

Each command starts from the active resume. To apply several operations together,
use one changes file. To continue editing an output, parse that output first.

`render` uses the saved original as its template. A render without edits produces
a byte-for-byte copy and requires a new output path. To edit a DOCX, pass
`--edits changes.json` with the source fingerprint and operations using IDs
from the compact view. Apply changes after reviewing the operation list.

```json
{
  "source_sha256": "<source_sha256>",
  "operations": [
    {"op": "replace", "id": "p3", "text": "Replacement text"},
    {"op": "add", "id": "p3", "template_id": "p2", "text": "Projects"},
    {"op": "remove", "id": "p4"}
  ]
}
```

`add` inserts after the target paragraph and copies formatting from
`template_id`, which defaults to the target. For a new heading, use the
existing heading as the template and the desired insertion point as the target.
To edit a whole section, list operations for its heading and relevant paragraphs.
Mixed-format replacements require the existing run-level edit interface;
use `format` to emphasize existing text without replacing the paragraph.
Paragraph operations reject targets with embedded content such as hyperlinks
or images. IDs refer to the parsed source version. To make an edited output
the active resume, parse it.
Word can repaginate when the replacement text changes length.

### Bold and italic

The `text` field accepts `\b{bold}`, `\i{italic}`, and nested
`\b{\i{bold italic}}` in additions, replacements, and formatting-only edits.
Unmarked text inherits the source formatting. Escape literal backslashes as
`\\` and literal braces as `\{` and `\}`. Invalid or unclosed markup is rejected.
JSON requires each backslash to be doubled:

```json
{"op": "format", "id": "p3", "match": "reports", "text": "\\b{reports}"}
```

`format` preserves the exact wording and existing formatting outside the marked
range. Its `text`, after removing markup, must equal `match`. Omit `match` to
format the entire item. For repeated phrases, supply `"occurrence": 2` (or
`--occurrence 2` in the CLI) to select a one-based occurrence; ambiguous matches
are rejected. Formatting-only DOCX edits preserve individual runs' other properties.
Paragraphs with embedded objects or hyperlinks remain unsupported targets.

PDF and DOCX use the same `add`, `remove`, `replace`, and `format` operation payloads.
By default, PDF replacement retains the original line positions and uses
obstacle-aware clear column width. Addition copies the template's style and
indentation into clear space immediately below the target, using the destination
width. Removal leaves the original space empty. Fixed layout rejects edits that
need extra lines or vertical movement. It uses a standard PDF font based on the
dominant template style, with bold and italic variants for inline markup.
Formatting-only edits retain each source span's size, color, and bold/italic
style, mapped to standard PDF fonts; glyph shapes can differ. Formatting may use
clear horizontal space within the page margins and is rejected if it cannot fit.
Bullet markers are copied from the source glyphs to preserve their shape, font,
and indentation. Shorter replacements close unused trailing line positions when
the page permits safe compaction; URI links follow the moved text. Uniform page
backgrounds are preserved. Pages with annotations, forms, or internal links keep
their original positions when compaction cannot safely preserve those features.
OCR and DOCX-to-PDF conversion are not supported.

### PDF preflight and explicit reflow

```powershell
openresume preflight --edits changes.json
openresume preflight --edits changes.json --layout reflow
openresume render tailored.pdf --edits changes.json --layout reflow
```

Preflight renders in memory and writes no output. It returns JSON and exits with
status 1 for an invalid batch. Fixed-layout diagnostics include every independently
failing operation's index, ID, action, reason, available widths, line count,
remaining text and whether movement is required. Batch conflicts are also checked.
Rendering uses the same checks and never overwrites an existing path.

Explicit `--layout reflow` wraps edited paragraphs, preserves hanging bullet
indentation, shifts following rows and adds pages. Recognized headings stay with
following text. Untouched source glyphs, horizontal rules, solid backgrounds and
URI links are preserved; edited text uses standard PDF fonts. The renderer
validates link destinations and page bounds. Reflow changes spacing and page count,
so review the layout choice along with the wording and inspect output previews.
It handles single-column horizontal text and aligned dates. Images, complex
drawings, forms, annotations, internal links, rotated/offset-cropped pages and
detected prose columns fail explicitly. A replacement sharing its horizontal band
with other text or containing multiple links also needs fixed layout or DOCX.

Insert a complete section atomically with a JSON batch operation:

```json
{"op": "add_section", "id": "<anchor-id>", "heading": "Professional Summary",
 "text": "Complete summary body.", "heading_template_id": "<heading-id>",
 "template_id": "<body-style-id>"}
```

For PDF, the body inherits font appearance and indentation without a bullet
marker. For DOCX, choose a non-list body template.
All IDs refer to the parsed source. Separate marker/body lines are associated
only when close together on the same baseline with an unambiguous body.
Python callers can pass `layout="reflow"` to `render_resume` or `ResumeState.render`;
`ResumeState.preflight(payload, layout="reflow")` returns diagnostics.

Python callers can import `parse_resume` and `render_resume` from `openresume`.
Install pytest in the development environment and run `python -m pytest`.
