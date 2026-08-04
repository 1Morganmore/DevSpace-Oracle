from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_skill_metadata.py"


def load_validator():
    spec = importlib.util.spec_from_file_location("skill_metadata_validator_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_all_skill_frontmatter_parses_and_has_unique_required_metadata() -> None:
    validator = load_validator()

    assert validator.validate_skills(ROOT) == []


def test_yaml_parser_rejects_the_unquoted_colon_shape_that_broke_discovery(tmp_path: Path) -> None:
    validator = load_validator()
    skill = tmp_path / "skills" / "broken" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: broken\ndescription: broken prefix: unquoted suffix\n---\n",
        encoding="utf-8",
    )

    errors = validator.validate_skills(tmp_path)

    assert len(errors) == 1
    assert "mapping values are not allowed here" in errors[0]
