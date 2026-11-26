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
