"""Local resume parsing and rendering."""

import argparse
import json
from pathlib import Path
from zipfile import BadZipFile

from docx.opc.exceptions import PackageNotFoundError
from lxml.etree import XMLSyntaxError

from .engine import parse_resume, render_resume
from .state import ResumeState

__all__ = ["parse_resume", "render_resume"]


def main() -> None:
    parser = argparse.ArgumentParser(prog="openresume")
    parser.add_argument("--state-dir", type=Path, default=Path(".openresume"))
    commands = parser.add_subparsers(dest="command", required=True)
    parse = commands.add_parser("parse", help="Save a resume and return its compact agent view")
    parse.add_argument("source", type=Path)
    commands.add_parser("view", help="Return the active resume's compact agent view")
    render = commands.add_parser("render", help="Generate a resume from its original file")
    render.add_argument("output", type=Path)
    render.add_argument("--edits", type=Path, help="JSON file containing DOCX edit operations")
    preflight = commands.add_parser("preflight", help="Check every edit without writing an output")
    preflight.add_argument("--edits", type=Path, required=True)
    edit = commands.add_parser("edit", help="Apply one operation to the active resume")
    edit.add_argument("operation", choices=("replace", "add", "remove", "format"))
    edit.add_argument("id", help="Target item ID from the view command")
    edit.add_argument("--text", help="Text with optional \\b{bold} and \\i{italic} markup")
    edit.add_argument("--match", help="Exact existing phrase for a formatting-only edit")
    edit.add_argument("--occurrence", type=int, help="One-based occurrence of a repeated formatting match")
    edit.add_argument("--template-id", help="Item whose style an addition should copy")
    edit.add_argument("--output", type=Path, required=True, help="Path for the edited resume")
    for command in (edit, render, preflight):
        command.add_argument("--layout", choices=("fixed", "reflow"), default="fixed",
                             help="PDF fixed positions (default) or explicit single-column reflow")
    args = parser.parse_args()

    try:
        state = ResumeState(args.state_dir)
        if args.command == "parse":
            result = state.parse(args.source)
        elif args.command == "view":
            result = state.view()
        elif args.command == "edit":
            operation = {"op": args.operation, "id": args.id}
            if args.text is not None:
                operation["text"] = args.text
            if args.template_id is not None:
                operation["template_id"] = args.template_id
            if args.match is not None:
                operation["match"] = args.match
            if args.occurrence is not None:
                operation["occurrence"] = args.occurrence
            result = {"output": str(state.render(args.output, operations=[operation], layout=args.layout))}
        else:
            payload = None
            if args.edits:
                payload = json.loads(args.edits.read_text(encoding="utf-8-sig"))
                if (
                    not isinstance(payload, dict)
                    or not isinstance(payload.get("source_sha256"), str)
                    or not (
                        isinstance(payload.get("operations"), list) and "edits" not in payload
                        or isinstance(payload.get("edits"), dict) and "operations" not in payload
                    )
                ):
                    raise ValueError("Expected source_sha256 and either an operations list or an edits object.")
            if args.command == "preflight":
                result = state.preflight(payload, layout=args.layout)
            else:
                result = {"output": str(state.render(args.output, payload, layout=args.layout))}
        print(json.dumps(result, indent=2))
        if args.command == "preflight" and not result["valid"]:
            raise SystemExit(1)
    except (OSError, ValueError, KeyError, RuntimeError, BadZipFile,
            PackageNotFoundError, XMLSyntaxError) as error:
        parser.exit(1, f"openresume: {error}\n")
