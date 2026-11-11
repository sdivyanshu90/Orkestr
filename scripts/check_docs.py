"""Validate maintained Markdown links, anchors and table structure offline."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


def pages() -> list[Path]:
    return sorted(
        [
            ROOT / name
            for name in (
                "README.md",
                "PROJECT_STATUS.md",
                "flagship-contribution-prompt.md",
                "examples/README.md",
                "dashboard/README.md",
            )
        ]
        + list((ROOT / "docs").rglob("*.md"))
    )


def prose(text: str) -> list[tuple[int, str]]:
    result = []
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)[0]
            if fence is None:
                fence = token
            elif fence == token:
                fence = None
            continue
        if fence is None:
            result.append((number, line))
    return result


def anchors(path: Path) -> set[str]:
    found = set()
    duplicates: dict[str, int] = {}
    for _, line in prose(path.read_text(encoding="utf-8")):
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
        if not match:
            continue
        title = re.sub(r"<[^>]+>", "", match.group(1)).lower()
        slug = "".join(
            c for c in title if c in ("-", "_", " ") or unicodedata.category(c)[0] in ("L", "N")
        )
        slug = slug.replace(" ", "-")
        count = duplicates.get(slug, 0)
        duplicates[slug] = count + 1
        found.add(f"{slug}-{count}" if count else slug)
    return found


def check() -> list[str]:
    errors = []
    anchor_cache: dict[Path, set[str]] = {}
    for path in pages():
        text = path.read_text(encoding="utf-8")
        label = path.relative_to(ROOT)
        if not text.endswith("\n"):
            errors.append(f"{label}: missing final newline")
        width = None
        for number, line in prose(text):
            if line != line.rstrip():
                errors.append(f"{label}:{number}: trailing whitespace")
            if line.startswith("|"):
                size = len(re.split(r"(?<!\\)\|", line))
                if width is not None and size != width:
                    errors.append(
                        f"{label}:{number}: table has {size - 2} cells, expected {width - 2}"
                    )
                width = size
            else:
                width = None
            for raw in re.findall(r"!?\[[^\]\n]*\]\(([^)\n]+)\)", line):
                target = raw.strip().split(' "', 1)[0].strip("<>")
                if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
                    continue
                name, _, fragment = target.partition("#")
                resolved = (path.parent / unquote(name)).resolve() if name else path.resolve()
                if not resolved.exists():
                    errors.append(f"{label}:{number}: missing target {target}")
                    continue
                if fragment and resolved.suffix == ".md":
                    if resolved not in anchor_cache:
                        anchor_cache[resolved] = anchors(resolved)
                    if unquote(fragment) not in anchor_cache[resolved]:
                        errors.append(f"{label}:{number}: missing anchor {target}")
    return errors


if __name__ == "__main__":
    problems = check()
    for problem in problems:
        print(problem)
    if problems:
        raise SystemExit(1)
    print(f"Validated {len(pages())} Markdown pages: links, anchors, tables and whitespace.")
