#!/usr/bin/env python3
"""Validate MesoForge architecture-decision-record and doc conventions.

Checks:
- every ADR under docs/decisions/ has a unique four-digit number prefix;
- every ADR has ``Status: Accepted`` and the required section headings
  (``## Context``, ``## Decision``, ``## Consequences``);
- the four canonical root documents and retained data contracts exist;
- relative markdown links inside canonical root documents and docs/ resolve to files.

Exits non-zero with a description of every problem found (not just the
first) so failures are fixable in one pass.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REQUIRED_ADR_HEADINGS = ("## Context", "## Decision", "## Consequences")
CANONICAL_DOCS = tuple(
    Path(name) for name in ("README.md", "VISION.md", "ARCHITECTURE.md", "AGENTS.md")
)
REQUIRED_DOCS = CANONICAL_DOCS + (
    Path("docs/data-contracts/vocabulary.md"),
    Path("docs/data-contracts/phase-0.md"),
    Path("docs/data-contracts/phase-1.md"),
    Path("docs/data-contracts/phase-2.md"),
)
ADR_NUMBER_RE = re.compile(r"^(\d{4})-")
STATUS_RE = re.compile(r"^Status:\s*(\S+)", re.MULTILINE)
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _is_external_or_anchor(target: str) -> bool:
    if target.startswith(("http://", "https://", "mailto:", "#")):
        return True
    return False


def validate_adrs(root: Path) -> list[str]:
    errors: list[str] = []
    decisions_dir = root / "docs" / "decisions"
    if not decisions_dir.is_dir():
        return [f"missing required directory: {decisions_dir}"]

    seen_numbers: dict[str, Path] = {}
    for adr_path in sorted(decisions_dir.glob("*.md")):
        match = ADR_NUMBER_RE.match(adr_path.name)
        if not match:
            errors.append(f"{adr_path}: filename must start with a 4-digit ADR number")
            continue
        number = match.group(1)
        if number in seen_numbers:
            errors.append(
                f"{adr_path}: duplicate ADR number {number!r} (also used by {seen_numbers[number]})"
            )
        else:
            seen_numbers[number] = adr_path

        text = adr_path.read_text(encoding="utf-8")

        status_match = STATUS_RE.search(text)
        if not status_match or status_match.group(1) != "Accepted":
            found = status_match.group(1) if status_match else "<missing>"
            errors.append(f"{adr_path}: Status must be 'Accepted', found {found!r}")

        for heading in REQUIRED_ADR_HEADINGS:
            if heading not in text:
                errors.append(f"{adr_path}: missing required heading {heading!r}")

    return errors


def validate_required_docs(root: Path) -> list[str]:
    errors = []
    for rel_path in REQUIRED_DOCS:
        if not (root / rel_path).is_file():
            errors.append(f"missing required doc: {rel_path}")
    return errors


def validate_links(root: Path) -> list[str]:
    errors: list[str] = []
    docs_dir = root / "docs"
    markdown_paths = list(docs_dir.rglob("*.md")) if docs_dir.is_dir() else []
    markdown_paths.extend(root / path for path in CANONICAL_DOCS if (root / path).is_file())

    for md_path in sorted(markdown_paths):
        text = md_path.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK_RE.finditer(text):
            target = match.group(1).strip()
            if _is_external_or_anchor(target):
                continue
            target_path_str = target.split("#", 1)[0]
            if not target_path_str:
                continue
            resolved = (md_path.parent / target_path_str).resolve()
            if not resolved.is_file():
                errors.append(
                    f"{md_path}: broken relative link -> {target!r} "
                    f"(resolved to {resolved}, which does not exist)"
                )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root to validate (defaults to the real repo root).",
    )
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()

    errors: list[str] = []
    errors.extend(validate_required_docs(root))
    errors.extend(validate_adrs(root))
    errors.extend(validate_links(root))

    if errors:
        print(f"docs validation failed with {len(errors)} problem(s):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("docs validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
