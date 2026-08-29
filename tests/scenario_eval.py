"""Offline-first scenario eval for Josty live-agent usefulness.

Default: score ``tests/scenario_corpus.jsonl`` against ``scenario_queries.SCENARIOS``.
Optional live recapture (not CI):

    JOSTY_LIVE_EVAL=1 python tests/scenario_eval.py --live --out tests/scenario_out/live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / ".agents" / "skills" / "josty" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scenario_queries import SCENARIOS  # noqa: E402

DEFAULT_CORPUS = HERE / "scenario_corpus.jsonl"
DEFAULT_OUT = HERE / "scenario_out" / "replay"

CONTENT_KEEP_CHARS = 400


def host_of(url: str) -> str:
    hostname = (urlsplit(url or "").hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname


def host_matches(url: str, hosts: tuple[str, ...] | list[str]) -> bool:
    hostname = host_of(url)
    if not hostname:
        return False
    return any(hostname == site or hostname.endswith(f".{site}") for site in hosts)


def result_text(item: dict[str, Any], *, include_content: bool = False) -> str:
    parts = [item.get("title") or "", item.get("snippet") or ""]
    if include_content:
        parts.append(item.get("content") or "")
    return " ".join(parts).lower()


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop bulky fetch bodies so the checked-in corpus stays small."""
    clone = json.loads(json.dumps(payload))
    for item in clone.get("results") or []:
        content = item.get("content")
        if isinstance(content, str) and len(content) > CONTENT_KEEP_CHARS:
            item["content"] = content[:CONTENT_KEEP_CHARS]
        error = item.get("fetch_error")
        if isinstance(error, str) and len(error) > 240:
            item["fetch_error"] = error[:240]
    return clone


@dataclass
class CaseResult:
    id: str
    layer: str
    verdict: str
    taxonomy_class: str | None
    pathway: str
    issues: list[str] = field(default_factory=list)
    confidence: str = "reproduced"

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_payload(spec: dict[str, Any], payload: dict[str, Any]) -> CaseResult:
    issues: list[str] = []
    if spec.get("diagnose") or spec["flags"].get("diagnose"):
        issues.extend(_evaluate_diagnose(spec, payload))
    else:
        issues.extend(_evaluate_search(spec, payload))
    failed = bool(issues)
    return CaseResult(
        id=spec["id"],
        layer=spec["layer"],
        verdict="fail" if failed else "pass",
        taxonomy_class=spec["label_if_fail"] if failed else None,
        pathway=spec["pathway"],
        issues=issues,
    )


def _evaluate_diagnose(spec: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if payload.get("schema_version") != "1.0":
        issues.append(f"schema_version={payload.get('schema_version')!r}")
    if spec.get("require_reachable_field") and "reachable" not in payload:
        issues.append("missing reachable field")
    providers = payload.get("providers") or []
    if len(providers) < spec.get("min_hosts", 1):
        issues.append(f"expected >= {spec.get('min_hosts', 1)} hosts, got {len(providers)}")
    if spec.get("http_error_still_ok"):
        for item in providers:
            status = item.get("http_status")
            if isinstance(status, int) and status >= 400 and not item.get("ok"):
                issues.append(
                    f"{item.get('provider')} http_status={status} ok=false "
                    "(diagnose treats any HTTP response as reachable)"
                )
    return issues


def _evaluate_search(spec: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if payload.get("schema_version") != "1.0":
        issues.append(f"schema_version={payload.get('schema_version')!r}")
    expected_status = spec.get("expect_status")
    if expected_status and payload.get("status") != expected_status:
        issues.append(f"status={payload.get('status')!r} expected {expected_status!r}")
    rows = payload.get("results") or []
    min_results = spec.get("min_results", 0)
    if len(rows) < min_results:
        issues.append(f"expected >= {min_results} results, got {len(rows)}")
    if payload.get("count") != len(rows):
        issues.append(f"count={payload.get('count')} != len(results)={len(rows)}")

    allowed = spec.get("allowed_hosts")
    if allowed:
        for item in rows:
            url = item.get("url") or ""
            if url and not host_matches(url, allowed):
                issues.append(f"site leak: {host_of(url)}")

    must_hosts = spec.get("must_hosts")
    if must_hosts and rows and not any(
        host_matches(item.get("url") or "", must_hosts) for item in rows
    ):
        issues.append(f"no result on required hosts {tuple(must_hosts)[:6]}…")

    include_content = bool(spec.get("search_content"))
    must_answer = spec.get("must_answer") or []
    if must_answer and rows:
        blob = " ".join(result_text(item, include_content=include_content) for item in rows)
        missing = [token for token in must_answer if token.lower() not in blob]
        if missing:
            issues.append(f"missing answer tokens {missing}")

    forbid_if_missing = spec.get("forbid_if_missing_must") or []
    if forbid_if_missing and must_answer and rows:
        blob = " ".join(result_text(item, include_content=include_content) for item in rows)
        has_must = all(token.lower() in blob for token in must_answer)
        if not has_must:
            for token in forbid_if_missing:
                if token.lower() in blob:
                    issues.append(
                        f"near-miss: found {token!r} but missing {must_answer}"
                    )

    if spec.get("fetch_content_or_error") and rows:
        useful = [
            item
            for item in rows
            if (item.get("content") and len(item.get("content") or "") > 20)
            or item.get("fetch_error")
        ]
        if not useful:
            issues.append("fetch produced neither content nor fetch_error")

    if spec.get("require_empty_ok_provider"):
        providers = payload.get("providers") or []
        if not providers or any(not item.get("ok") for item in providers):
            issues.append("expected all providers ok")
        if not any(item.get("ok") and item.get("result_count") == 0 for item in providers):
            issues.append("expected an ok provider with result_count=0")
    return issues


def load_corpus(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            rows[row["id"]] = row
    return rows


def evaluate_corpus(corpus: dict[str, dict[str, Any]]) -> list[CaseResult]:
    results: list[CaseResult] = []
    for spec in SCENARIOS:
        row = corpus.get(spec["id"])
        if row is None:
            results.append(
                CaseResult(
                    id=spec["id"],
                    layer=spec["layer"],
                    verdict="fail",
                    taxonomy_class="contract_bug",
                    pathway=spec["pathway"],
                    issues=["missing corpus row"],
                    confidence="once",
                )
            )
            continue
        results.append(evaluate_payload(spec, row["payload"]))
    return results


def render_report(results: list[CaseResult]) -> str:
    failed = [row for row in results if row.verdict != "pass"]
    lines = [
        "# Josty scenario eval",
        "",
        "Constraint checks against the frozen scenario corpus. "
        "Failures emit a taxonomy class from `docs/ISSUE_TAXONOMY.md`.",
        "",
        f"**{sum(row.verdict == 'pass' for row in results)}/{len(results)} constraints passed.**",
        "",
        "| Case | Layer | Verdict | Class | Pathway |",
        "|---|---|---|---|---|",
    ]
    for row in results:
        klass = row.taxonomy_class or "—"
        note = row.pathway.replace("|", "/")
        lines.append(
            f"| `{row.id}` | {row.layer} | **{row.verdict}** | `{klass}` | {note} |"
        )
    lines += ["", "## Issues", ""]
    if not failed:
        lines.append("No constraint failures.")
    else:
        for row in failed:
            detail = "; ".join(row.issues) or "unspecified"
            lines.append(f"- `{row.id}` ({row.taxonomy_class}): {detail}")
    return "\n".join(lines) + "\n"


def write_outputs(out_dir: Path, results: list[CaseResult]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "REPORT.md").write_text(render_report(results), encoding="utf-8")
    (out_dir / "results.json").write_text(
        json.dumps([row.as_row() for row in results], indent=2),
        encoding="utf-8",
    )


async def _live_payload(spec: dict[str, Any]) -> dict[str, Any]:
    from josty.engine import Josty

    flags = spec.get("flags") or {}
    engine = Josty(
        enable_cache=False,
        max_content_chars=flags.get("max_content_chars", 8000),
    )
    if flags.get("diagnose") or spec.get("diagnose"):
        return (await engine.diagnose_run()).dict()
    run = await engine.research_run(
        spec["query"],
        sites=flags.get("sites"),
        mode=flags.get("mode", "plain"),
        limit=flags.get("limit", 5),
        fetch=bool(flags.get("fetch")),
        category=flags.get("category", "text"),
        timelimit=flags.get("timelimit"),
        profile=flags.get("profile", "general"),
    )
    return run.dict()


def capture_live(out_dir: Path) -> Path:
    if spec_skip := [s["id"] for s in SCENARIOS if s.get("live") is False]:
        print(f"skipping non-live fixtures: {', '.join(spec_skip)}", file=sys.stderr)
    captured_at = datetime.now(timezone.utc).isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "scenario_corpus.live.jsonl"
    rows: list[dict[str, Any]] = []
    for spec in SCENARIOS:
        if spec.get("live") is False:
            continue
        print(f"live {spec['id']}", file=sys.stderr)
        payload = asyncio.run(_live_payload(spec))
        rows.append(
            {
                "id": spec["id"],
                "captured_at": captured_at,
                "payload": sanitize_payload(payload),
            }
        )
    dest.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Josty scenario eval")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--live",
        action="store_true",
        help="recapture live envelopes (requires JOSTY_LIVE_EVAL=1); "
        "does not overwrite the checked-in corpus",
    )
    args = parser.parse_args(argv)
    if args.live:
        if os.environ.get("JOSTY_LIVE_EVAL") != "1":
            print("refusing --live without JOSTY_LIVE_EVAL=1", file=sys.stderr)
            return 2
        live_path = capture_live(args.out)
        print(f"wrote {live_path}", file=sys.stderr)
        corpus = load_corpus(args.corpus)
        corpus.update(load_corpus(live_path))
    else:
        corpus = load_corpus(args.corpus)
    results = evaluate_corpus(corpus)
    write_outputs(args.out, results)
    print(render_report(results), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
