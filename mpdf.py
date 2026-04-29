#!/usr/bin/env python3
"""Windows-first Markdown-to-PDF CLI using Pandoc and XeLaTeX."""

from __future__ import annotations

import argparse
import codecs
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date
from pathlib import Path


DEFAULT_AUTHOR = "Prajas Wadekar"
FALLBACK_INPUT_FORMAT = "markdown-yaml_metadata_block-simple_tables-multiline_tables-grid_tables"


class MpdfError(Exception):
    """Raised when conversion cannot proceed."""


@dataclass
class Metadata:
    title: str
    author: str
    date: str


@dataclass
class InputConfig:
    yaml_meta: dict[str, str]
    input_format: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mpdf",
        description="Convert Markdown files to polished PDFs with Pandoc and XeLaTeX.",
    )
    parser.add_argument("input", help="Path to the Markdown file to convert.")
    parser.add_argument("title", nargs="?", help="Override the document title.")
    parser.add_argument("author", nargs="?", help="Override the document author.")
    parser.add_argument("date", nargs="?", help="Override the document date.")
    parser.add_argument(
        "--no-toc",
        action="store_true",
        help="Disable the table of contents.",
    )
    parser.add_argument(
        "--template",
        help="Path to a custom Pandoc LaTeX template.",
    )
    parser.add_argument(
        "--title-page",
        action="store_true",
        help="Place the title on its own page before the table of contents.",
    )

    args = parser.parse_args()
    args.input = Path(args.input).expanduser().resolve()

    if args.template:
        args.template = Path(args.template).expanduser().resolve()

    return args


def check_dependencies() -> None:
    missing = []

    if shutil.which("pandoc") is None:
        missing.append(
            "Pandoc was not found on PATH. Install it from https://pandoc.org/installing.html "
            "or with `winget install --id Pandoc.Pandoc`."
        )

    if shutil.which("xelatex") is None:
        missing.append(
            "XeLaTeX was not found on PATH. Install TeX Live or ensure the `xelatex` executable "
            "is available in your PATH."
        )

    if missing:
        raise MpdfError("\n".join(missing))


def require_input_file(input_path: Path) -> None:
    if not input_path.exists():
        raise MpdfError(f"Input file not found: {input_path}")
    if not input_path.is_file():
        raise MpdfError(f"Input path is not a file: {input_path}")


def detect_input_config(input_path: Path) -> InputConfig:
    command = ["pandoc", str(input_path), "-t", "json"]
    result = subprocess.run(
        command,
        cwd=input_path.parent,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if is_yaml_metadata_error(result.stderr):
            if starts_with_yaml_front_matter(input_path):
                raise MpdfError(
                    "Your Markdown file starts with a YAML front matter block, but it is not valid YAML. "
                    "Fix or remove the front matter and try again.\n\n"
                    f"{format_command_error(command, result.stderr, result.stdout)}"
                )

            fallback_command = [
                "pandoc",
                "--from",
                FALLBACK_INPUT_FORMAT,
                str(input_path),
                "-t",
                "json",
            ]
            fallback_result = subprocess.run(
                fallback_command,
                cwd=input_path.parent,
                capture_output=True,
                text=True,
            )
            if fallback_result.returncode != 0:
                raise MpdfError(
                    "Pandoc could not read the Markdown file, even after retrying with YAML metadata parsing disabled.\n"
                    f"{format_command_error(fallback_command, fallback_result.stderr, fallback_result.stdout)}"
                )

            return InputConfig(
                yaml_meta=extract_metadata_from_json(fallback_result.stdout),
                input_format=FALLBACK_INPUT_FORMAT,
            )

        raise MpdfError(
            "Pandoc could not read the Markdown metadata.\n"
            f"{format_command_error(command, result.stderr, result.stdout)}"
        )

    return InputConfig(yaml_meta=extract_metadata_from_json(result.stdout))


def extract_metadata_from_json(document_json: str) -> dict[str, str]:
    try:
        document = json.loads(document_json)
    except json.JSONDecodeError as exc:
        raise MpdfError(f"Pandoc returned invalid JSON while probing metadata: {exc}") from exc

    meta = document.get("meta", {})
    extracted = {}
    for field in ("title", "author", "date"):
        if field in meta:
            text = meta_value_to_text(meta[field]).strip()
            if text:
                extracted[field] = text

    return extracted


def is_yaml_metadata_error(stderr: str) -> bool:
    if not stderr:
        return False
    lowered = stderr.lower()
    return "error parsing yaml metadata" in lowered or "yaml parse exception" in lowered


def starts_with_yaml_front_matter(input_path: Path) -> bool:
    try:
        with input_path.open("rb") as handle:
            sample = handle.read(4096)
    except OSError:
        return False

    if sample.startswith(codecs.BOM_UTF8):
        sample = sample[len(codecs.BOM_UTF8) :]

    first_line = sample.splitlines()[0] if sample.splitlines() else b""
    return first_line.strip() == b"---"


def meta_value_to_text(node: object) -> str:
    if not isinstance(node, dict):
        return ""

    node_type = node.get("t")
    content = node.get("c")

    if node_type == "MetaString":
        return str(content)
    if node_type == "MetaBool":
        return "true" if content else "false"
    if node_type == "MetaInlines":
        return inlines_to_text(content)
    if node_type == "MetaBlocks":
        return blocks_to_text(content)
    if node_type == "MetaList":
        parts = [meta_value_to_text(item).strip() for item in content]
        return ", ".join(part for part in parts if part)
    if node_type == "MetaMap":
        return ""

    return ""


def blocks_to_text(blocks: object) -> str:
    if not isinstance(blocks, list):
        return ""

    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("t")
        block_content = block.get("c")

        if block_type in {"Plain", "Para"} and isinstance(block_content, list):
            parts.append(inlines_to_text(block_content))
        elif block_type == "Header" and isinstance(block_content, list) and len(block_content) >= 3:
            parts.append(inlines_to_text(block_content[2]))

    return "\n".join(part.strip() for part in parts if part.strip())


def inlines_to_text(inlines: object) -> str:
    if not isinstance(inlines, list):
        return ""

    pieces = []
    for inline in inlines:
        pieces.append(inline_to_text(inline))

    return normalize_whitespace("".join(pieces))


def inline_to_text(inline: object) -> str:
    if not isinstance(inline, dict):
        return ""

    inline_type = inline.get("t")
    content = inline.get("c")

    if inline_type == "Str":
        return str(content)
    if inline_type in {"Space", "SoftBreak", "LineBreak"}:
        return " "
    if inline_type in {"Emph", "Strong", "Strikeout", "SmallCaps", "Quoted", "Underline"}:
        if isinstance(content, list) and content and inline_type == "Quoted":
            return inlines_to_text(content[1])
        return inlines_to_text(content)
    if inline_type == "Superscript" or inline_type == "Subscript":
        return inlines_to_text(content)
    if inline_type == "Code":
        if isinstance(content, list) and len(content) == 2:
            return str(content[1])
    if inline_type == "Math":
        if isinstance(content, list) and len(content) == 2:
            return str(content[1])
    if inline_type in {"Link", "Image", "Span"}:
        if isinstance(content, list) and len(content) >= 2:
            return inlines_to_text(content[1])
    if inline_type == "Note":
        return ""
    if inline_type == "Cite":
        if isinstance(content, list) and len(content) == 2:
            return inlines_to_text(content[1])
    if inline_type == "RawInline":
        if isinstance(content, list) and len(content) == 2:
            return str(content[1])

    return ""


def normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def resolve_metadata(args: argparse.Namespace, yaml_meta: dict[str, str], input_path: Path) -> Metadata:
    return Metadata(
        title=args.title or yaml_meta.get("title") or input_path.stem,
        author=args.author or yaml_meta.get("author") or DEFAULT_AUTHOR,
        date=args.date or yaml_meta.get("date") or date.today().isoformat(),
    )


def build_pandoc_command(
    args: argparse.Namespace,
    metadata: Metadata,
    input_format: str | None,
    input_path: Path,
    output_path: Path,
    stack: ExitStack,
) -> list[str]:
    command = [
        "pandoc",
        "--standalone",
        "--pdf-engine=xelatex",
        "--syntax-highlighting=pygments",
        "--variable",
        "geometry:margin=1in",
        "--variable",
        "fontsize=11pt",
        "--variable",
        "linestretch=1.08",
        "--variable",
        "mainfont=Cambria",
        "--variable",
        "sansfont=Calibri",
        "--variable",
        "monofont=Consolas",
        "--variable",
        "colorlinks=true",
        "--variable",
        "linkcolor=blue",
        "--variable",
        "urlcolor=blue",
        "--metadata",
        f"title={metadata.title}",
        "--metadata",
        f"author={metadata.author}",
        "--metadata",
        f"date={metadata.date}",
        "--output",
        str(output_path),
    ]

    if input_format:
        command.extend(["--from", input_format])

    command.append(str(input_path))

    if not args.no_toc:
        command.extend(["--toc", "--toc-depth=3"])

    if args.template:
        if not args.template.exists():
            raise MpdfError(f"Template file not found: {args.template}")
        if not args.template.is_file():
            raise MpdfError(f"Template path is not a file: {args.template}")
        command.extend(["--template", str(args.template)])
    else:
        header_file = write_temp_file(stack, "mpdf-header-", ".tex", builtin_header())
        command.extend(["--include-in-header", header_file])

    if args.title_page:
        before_body_file = write_temp_file(stack, "mpdf-before-", ".tex", "\\clearpage\n")
        command.extend(["--include-before-body", before_body_file])

    return command


def write_temp_file(stack: ExitStack, prefix: str, suffix: str, content: str) -> str:
    fd, temp_name = tempfile.mkstemp(prefix=prefix, suffix=suffix, text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
        temp_file.write(content)
    temp_path = Path(temp_name)
    stack.callback(lambda path=temp_path: path.unlink(missing_ok=True))
    return str(temp_path)


def builtin_header() -> str:
    return r"""
\usepackage{framed}
\definecolor{mpdfcodebg}{HTML}{F7F7F7}
\definecolor{mpdfheading}{HTML}{1F2937}
\definecolor{mpdflink}{HTML}{1D4ED8}
\colorlet{shadecolor}{mpdfcodebg}
\makeatletter
\AtBeginDocument{
  \hypersetup{colorlinks=true, linkcolor=mpdflink, urlcolor=mpdflink}
  \@ifundefined{Shaded}{}{\renewenvironment{Shaded}{\begin{snugshade}}{\end{snugshade}}}
  \@ifundefined{Highlighting}{}{\DefineVerbatimEnvironment{Highlighting}{Verbatim}{commandchars=\\\{\},fontsize=\small}}
}
\IfFileExists{titling.sty}{
  \usepackage{titling}
  \setlength{\droptitle}{-2.5em}
  \pretitle{\begin{center}\LARGE\bfseries\color{mpdfheading}}
  \posttitle{\par\end{center}\vspace{0.6em}}
  \preauthor{\begin{center}\large}
  \postauthor{\par\end{center}\vspace{0.3em}}
  \predate{\begin{center}\normalsize}
  \postdate{\par\end{center}\vspace{1.2em}}
}{}
\IfFileExists{sectsty.sty}{
  \usepackage{sectsty}
  \sectionfont{\Large\bfseries\color{mpdfheading}}
  \subsectionfont{\large\bfseries\color{mpdfheading}}
  \subsubsectionfont{\normalsize\bfseries\color{mpdfheading}}
}{}
\makeatother
""".lstrip()


def run_conversion(command: list[str], cwd: Path) -> None:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MpdfError(
            "Pandoc/XeLaTeX conversion failed.\n"
            f"{format_command_error(command, result.stderr, result.stdout)}"
        )


def format_command_error(command: list[str], stderr: str, stdout: str) -> str:
    details = [f"Command: {' '.join(command)}"]
    stderr = (stderr or "").strip()
    stdout = (stdout or "").strip()

    if stderr:
        details.append(f"stderr:\n{stderr}")
    if stdout:
        details.append(f"stdout:\n{stdout}")

    return "\n\n".join(details)


def main() -> int:
    try:
        args = parse_args()
        require_input_file(args.input)
        check_dependencies()

        input_config = detect_input_config(args.input)
        metadata = resolve_metadata(args, input_config.yaml_meta, args.input)
        output_path = args.input.with_suffix(".pdf")

        with ExitStack() as stack:
            command = build_pandoc_command(
                args,
                metadata,
                input_config.input_format,
                args.input,
                output_path,
                stack,
            )
            run_conversion(command, args.input.parent)

        print(f"Created PDF: {output_path}")
        return 0
    except MpdfError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Error: conversion cancelled by user.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
