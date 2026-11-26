"""The josty skill is the contract agents read first; it must not drift from the code."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

from josty.cli import parser
from josty.status import ErrorKind

SKILL = Path(__file__).resolve().parents[1] / ".agents" / "skills" / "josty" / "SKILL.md"
TICK = chr(96)


def test_every_error_kind_is_documented_and_a_removed_mode_stays_gone():
    text = SKILL.read_text()

    for kind in get_args(ErrorKind):
        assert TICK + kind + TICK in text, f"the skill does not document error_kind {kind}"

    mode = next(action for action in parser()._actions if action.dest == "mode")
    assert tuple(mode.choices) == ("plain", "exact")
    assert "--mode oss" not in text
    assert TICK + "oss" + TICK not in text


def test_a_filter_refusal_is_documented_as_not_applicable_not_skipped():
    """A strict safe-search refusal is not_applicable, like site, region and time limit."""
    readme = (SKILL.parents[3] / "README.md").read_text()

    for text in (SKILL.read_text(), readme):
        assert "mwmbl" in text
        assert "is skipped at" not in text
        assert "skips it too" not in text
