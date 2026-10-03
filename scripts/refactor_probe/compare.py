"""Compare two envelope-probe snapshots; exit 1 on any meaningful difference.

The probe is hermetic (mock DDGS + fake httpx), so any remaining difference is a
behavior change the refactor must justify.

Two quantities are non-deterministic by construction and are normalized, visibly:

* ``latency_ms`` -- real wall-clock jitter on a mock engine. Presence is still
  compared (None vs measured is a behavior signal); the value is not.
* absolute cool-down stamps inside a breaker skip message ("until <ISO>Z") -- the
  scenario records a real failure at run time. The surrounding text is compared.

Every other key is compared byte-for-byte. Run this on two snapshots of the SAME
code first: it must print IDENTICAL, or the normalization is wrong.

Usage: .venv/bin/python scripts/refactor_probe/compare.py base.json head.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

COOL_DOWN_RE = re.compile(r"until \d{4}-\d\d-\d\dT[\d:.]+Z")

STATS = {"latency_values": 0, "latency_nones": 0, "cool_down_stamps": 0}


def normalize(node):
    if isinstance(node, dict):
        return {key: normalize(value) for key, value in node.items()}
    if isinstance(node, list):
        return [normalize(item) for item in node]
    if isinstance(node, str):
        fixed, count = COOL_DOWN_RE.subn("until <TS>", node)
        STATS["cool_down_stamps"] += count
        return fixed
    return node


def normalize_latency(node):
    """Replace latency_ms values with a presence marker, in place of the value."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "latency_ms":
                if value is None:
                    STATS["latency_nones"] += 1
                    out[key] = None
                else:
                    STATS["latency_values"] += 1
                    out[key] = "<measured>"
            else:
                out[key] = normalize_latency(value)
        return out
    if isinstance(node, list):
        return [normalize_latency(item) for item in node]
    return node


def walk(node, path: str, out: dict) -> None:
    if isinstance(node, dict):
        for key in sorted(node):
            walk(node[key], f"{path}.{key}", out)
    elif isinstance(node, list):
        out[f"{path}[]"] = node
    else:
        out[path] = node


def main() -> int:
    left = normalize_latency(normalize(json.loads(Path(sys.argv[1]).read_text())))
    right = normalize_latency(normalize(json.loads(Path(sys.argv[2]).read_text())))
    failures: list[str] = []

    for scenario in sorted(set(left) | set(right)):
        if scenario not in left:
            failures.append(f"+ scenario only in new snapshot: {scenario}")
            continue
        if scenario not in right:
            failures.append(f"- scenario only in old snapshot: {scenario}")
            continue
        flat_left: dict = {}
        flat_right: dict = {}
        walk(left[scenario], scenario, flat_left)
        walk(right[scenario], scenario, flat_right)
        diffs = []
        for key in sorted(set(flat_left) | set(flat_right)):
            if key not in flat_left:
                diffs.append(f"  + {key} = {flat_right[key]!r}")
            elif key not in flat_right:
                diffs.append(f"  - {key} (was {flat_left[key]!r})")
            elif flat_left[key] != flat_right[key]:
                diffs.append(f"  ~ {key}: {flat_left[key]!r} -> {flat_right[key]!r}")
        if diffs:
            failures.append(f"[{scenario}]")
            failures.extend(diffs)

    note = (
        f"normalized: {STATS['latency_values']} measured latency values (presence kept), "
        f"{STATS['latency_nones']} null latencies, {STATS['cool_down_stamps']} cool-down stamps"
    )
    if failures:
        print("BEHAVIOR DIFF DETECTED")
        print("\n".join(failures))
        print(note)
        return 1
    print(f"IDENTICAL across {len(left)} scenarios")
    print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
