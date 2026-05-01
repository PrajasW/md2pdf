#!/usr/bin/env python3
"""Windows-first Markdown-to-PDF CLI using Pandoc and XeLaTeX."""

from __future__ import annotations

import argparse
import codecs
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date
from pathlib import Path


DEFAULT_AUTHOR = "Prajas Wadekar"
DEFAULT_INPUT_FORMAT = "markdown+hard_line_breaks+link_attributes"
FALLBACK_INPUT_FORMAT = (
    "markdown+hard_line_breaks+link_attributes"
    "-yaml_metadata_block-simple_tables-multiline_tables-grid_tables"
)
IMAGE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
}
OBSIDIAN_EMBED_PATTERN = re.compile(r"!\[\[([^\]]+)\]\]")
ATX_HEADING_PATTERN = re.compile(r"^[ \t]{0,3}#{1,6}(?:[ \t]+|$)")
FENCED_CODE_BLOCK_PATTERN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


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


@dataclass
class ObsidianContext:
    vault_root: Path | None
    attachment_folder: Path | None
    resolved_targets: dict[str, Path | None]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mpdf",
        description="Convert Markdown files to polished PDFs with Pandoc and XeLaTeX.",
    )
    parser.add_argument("input", help="Path to the Markdown file to convert.")
    parser.add_argument(
        "--title",
        help="Override the document title.",
    )
    parser.add_argument(
        "--author",
        help="Override the document author.",
    )
    parser.add_argument(
        "--date",
        help="Override the document date.",
    )
    parser.add_argument(
        "--index",
        action="store_true",
        help="Include a table of contents.",
    )
    parser.add_argument(
        "--template",
        help="Path to a custom Pandoc LaTeX template.",
    )
    parser.add_argument(
        "--title-page",
        action="store_true",
        help="Place the title on its own page before the table of contents or body.",
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
    command = ["pandoc", "--from", DEFAULT_INPUT_FORMAT, str(input_path), "-t", "json"]
    result = run_command(command, input_path.parent)
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
            fallback_result = run_command(fallback_command, input_path.parent)
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

    return InputConfig(
        yaml_meta=extract_metadata_from_json(result.stdout),
        input_format=DEFAULT_INPUT_FORMAT,
    )


def extract_metadata_from_json(document_json: str | bytes) -> dict[str, str]:
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


def is_yaml_metadata_error(stderr: str | bytes | None) -> bool:
    stderr_text = decode_output(stderr)
    if not stderr_text:
        return False
    lowered = stderr_text.lower()
    return "error parsing yaml metadata" in lowered or "yaml parse exception" in lowered


def run_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=False,
    )


def decode_output(data: str | bytes | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if not data:
        return ""

    encodings = ["utf-8"]
    preferred = locale.getpreferredencoding(False)
    if preferred and preferred.lower() not in {"utf-8", "utf8"}:
        encodings.append(preferred)
    if "cp1252" not in {enc.lower() for enc in encodings}:
        encodings.append("cp1252")

    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue

    return data.decode("utf-8", errors="replace")


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


def prepare_input_for_pandoc(input_path: Path, stack: ExitStack) -> Path:
    source_text = input_path.read_text(encoding="utf-8")
    obsidian_context = build_obsidian_context(input_path)
    rewritten_text = rewrite_obsidian_embeds(source_text, input_path, obsidian_context)
    normalized_text = normalize_heading_spacing(rewritten_text)

    if normalized_text == source_text:
        return input_path

    return Path(
        write_temp_file(
            stack,
            "mpdf-input-",
            ".md",
            normalized_text,
            directory=input_path.parent,
        )
    )


def normalize_heading_spacing(source_text: str) -> str:
    lines = source_text.splitlines()
    if not lines:
        return source_text

    normalized_lines: list[str] = []
    in_front_matter = False
    in_fenced_code_block = False
    active_fence = ""

    for index, line in enumerate(lines):
        stripped = line.strip()
        fence_match = FENCED_CODE_BLOCK_PATTERN.match(line)

        if index == 0 and stripped == "---":
            in_front_matter = True
            normalized_lines.append(line)
            continue

        if in_front_matter:
            normalized_lines.append(line)
            if stripped in {"---", "..."}:
                in_front_matter = False
            continue

        if fence_match:
            fence = fence_match.group(1)
            if not in_fenced_code_block:
                in_fenced_code_block = True
                active_fence = fence
            elif fence[0] == active_fence[0] and len(fence) >= len(active_fence):
                in_fenced_code_block = False
                active_fence = ""
            normalized_lines.append(line)
            continue

        if (
            not in_fenced_code_block
            and ATX_HEADING_PATTERN.match(line)
            and normalized_lines
            and normalized_lines[-1].strip()
        ):
            normalized_lines.append("")

        normalized_lines.append(line)

    normalized_text = "\n".join(normalized_lines)
    if source_text.endswith(("\n", "\r")):
        normalized_text += "\n"

    return normalized_text


def build_obsidian_context(input_path: Path) -> ObsidianContext:
    vault_root = find_obsidian_vault_root(input_path)
    attachment_folder = load_attachment_folder(vault_root)
    return ObsidianContext(
        vault_root=vault_root,
        attachment_folder=attachment_folder,
        resolved_targets={},
    )


def find_obsidian_vault_root(input_path: Path) -> Path | None:
    for directory in (input_path.parent, *input_path.parent.parents):
        if (directory / ".obsidian").is_dir():
            return directory
    return None


def load_attachment_folder(vault_root: Path | None) -> Path | None:
    if vault_root is None:
        return None

    config_path = vault_root / ".obsidian" / "app.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    folder = config.get("attachmentFolderPath")
    if not isinstance(folder, str):
        return None

    normalized = folder.strip().strip("/\\")
    if not normalized:
        return None

    return Path(normalized)


def rewrite_obsidian_embeds(source_text: str, input_path: Path, obsidian_context: ObsidianContext) -> str:
    def replace_embed(match: re.Match[str]) -> str:
        embed_markup = match.group(0)
        embed_target = match.group(1).strip()
        target_path, alt_text, size_spec = parse_obsidian_embed(embed_target)

        if not is_image_target(target_path):
            return embed_markup

        resolved_path = resolve_obsidian_asset(target_path, input_path, obsidian_context)
        if resolved_path is None:
            return embed_markup

        relative_path = os.path.relpath(resolved_path, start=input_path.parent).replace("\\", "/")
        image_markdown = f"![{sanitize_alt_text(alt_text)}](<{relative_path}>)"
        size_attributes = render_size_attributes(size_spec)
        return f"{image_markdown}{size_attributes}"

    return OBSIDIAN_EMBED_PATTERN.sub(replace_embed, source_text)


def parse_obsidian_embed(embed_target: str) -> tuple[str, str, str | None]:
    parts = [part.strip() for part in embed_target.split("|")]
    target_path = parts[0]
    alt_parts = []
    size_spec = None

    for part in parts[1:]:
        if not part:
            continue
        if size_spec is None and is_size_spec(part):
            size_spec = part
            continue
        alt_parts.append(part)

    target_path = target_path.split("#", 1)[0].strip()
    alt_text = " | ".join(alt_parts).strip()
    return target_path, alt_text, size_spec


def is_size_spec(value: str) -> bool:
    compact = value.replace(" ", "")
    return bool(re.fullmatch(r"\d+x\d+|\d+x|x\d+|\d+", compact))


def is_image_target(target_path: str) -> bool:
    return Path(target_path).suffix.lower() in IMAGE_EXTENSIONS


def resolve_obsidian_asset(
    target_path: str,
    input_path: Path,
    obsidian_context: ObsidianContext,
) -> Path | None:
    cached = obsidian_context.resolved_targets.get(target_path)
    if target_path in obsidian_context.resolved_targets:
        return cached

    note_directory = input_path.parent
    normalized_target = target_path.strip().replace("\\", "/")
    target_name = Path(normalized_target).name

    candidates: list[Path] = [
        note_directory / Path(normalized_target),
    ]

    if obsidian_context.vault_root is not None:
        candidates.append(obsidian_context.vault_root / Path(normalized_target))

    if obsidian_context.attachment_folder is not None:
        candidates.append(note_directory / obsidian_context.attachment_folder / target_name)
        if obsidian_context.vault_root is not None:
            candidates.append(obsidian_context.vault_root / obsidian_context.attachment_folder / target_name)

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            resolved = candidate.resolve()
            obsidian_context.resolved_targets[target_path] = resolved
            return resolved

    if obsidian_context.vault_root is not None:
        matches = [
            match.resolve()
            for match in obsidian_context.vault_root.rglob(target_name)
            if match.is_file()
        ]
        if len(matches) == 1:
            obsidian_context.resolved_targets[target_path] = matches[0]
            return matches[0]

    obsidian_context.resolved_targets[target_path] = None
    return None


def sanitize_alt_text(alt_text: str) -> str:
    return alt_text.replace("[", r"\[").replace("]", r"\]")


def render_size_attributes(size_spec: str | None) -> str:
    if not size_spec:
        return ""

    compact = size_spec.replace(" ", "")

    if re.fullmatch(r"\d+", compact):
        return f"{{width={compact}px}}"

    if re.fullmatch(r"\d+x\d+", compact):
        width, height = compact.split("x", 1)
        return f"{{width={width}px height={height}px}}"

    if re.fullmatch(r"\d+x", compact):
        width = compact[:-1]
        return f"{{width={width}px}}"

    if re.fullmatch(r"x\d+", compact):
        height = compact[1:]
        return f"{{height={height}px}}"

    return ""


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

    if args.index:
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


def write_temp_file(
    stack: ExitStack,
    prefix: str,
    suffix: str,
    content: str,
    directory: Path | None = None,
) -> str:
    temp_dir = str(directory) if directory is not None else None
    fd, temp_name = tempfile.mkstemp(prefix=prefix, suffix=suffix, text=True, dir=temp_dir)
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
    result = run_command(command, cwd)
    if result.returncode != 0:
        raise MpdfError(
            "Pandoc/XeLaTeX conversion failed.\n"
            f"{format_command_error(command, result.stderr, result.stdout)}"
        )


def format_command_error(command: list[str], stderr: str | bytes | None, stdout: str | bytes | None) -> str:
    details = [f"Command: {' '.join(command)}"]
    stderr_text = decode_output(stderr).strip()
    stdout_text = decode_output(stdout).strip()

    if stderr_text:
        details.append(f"stderr:\n{stderr_text}")
    if stdout_text:
        details.append(f"stdout:\n{stdout_text}")

    return "\n\n".join(details)


def main() -> int:
    try:
        args = parse_args()
        require_input_file(args.input)
        check_dependencies()

        with ExitStack() as stack:
            prepared_input = prepare_input_for_pandoc(args.input, stack)
            input_config = detect_input_config(prepared_input)
            metadata = resolve_metadata(args, input_config.yaml_meta, args.input)
            output_path = args.input.with_suffix(".pdf")
            command = build_pandoc_command(
                args,
                metadata,
                input_config.input_format,
                prepared_input,
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
