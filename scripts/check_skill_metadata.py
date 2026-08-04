#!/usr/bin/env python
"""Validate every repository skill's YAML frontmatter with a real parser."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


ALLOWED_KEYS = {"name", "description"}


def read_frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        raise ValueError("missing opening YAML frontmatter delimiter")
    normalized = text.replace("\r\n", "\n")
    parts = normalized.split("---\n", 2)
    if len(parts) != 3:
        raise ValueError("missing closing YAML frontmatter delimiter")
    value = yaml.safe_load(parts[1])
    if not isinstance(value, dict):
        raise ValueError("frontmatter must parse to a mapping")
    return value


def validate_skills(root: Path) -> list[str]:
    errors: list[str] = []
    names: dict[str, Path] = {}
    for path in sorted((root / "skills").glob("*/SKILL.md")):
        relative = path.relative_to(root).as_posix()
        try:
            value = read_frontmatter(path)
            extra = set(value) - ALLOWED_KEYS
            if extra:
                raise ValueError(f"unsupported keys: {', '.join(sorted(extra))}")
            name = value.get("name")
            description = value.get("description")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("name must be a nonempty string")
            if not isinstance(description, str) or not description.strip():
                raise ValueError("description must be a nonempty string")
            if name in names:
                raise ValueError(f"duplicate name also used by {names[name].relative_to(root).as_posix()}")
            names[name] = path
        except (OSError, UnicodeError, yaml.YAMLError, ValueError) as error:
            errors.append(f"{relative}: {error}")
    if not names and not errors:
        errors.append("no skills/*/SKILL.md files found")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    errors = validate_skills(args.root.resolve())
    if errors:
        for error in errors:
            print(f"SKILL_METADATA_INVALID {error}")
        return 1
    print("SKILL_METADATA_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
