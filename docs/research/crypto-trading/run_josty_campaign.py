#!/usr/bin/env python3
"""Orchestrate a full-capability Josty campaign for crypto trading research.

Concurrent flag sweep only. Sequential cache/limit/empty isolation is
`run_josty_isolation.py` — do not fold those jobs into the batches here.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw"
RAW.mkdir(parents=True, exist_ok=True)

# Each job: (id, extra_args_after_uvx_josty)
JOBS: list[tuple[str, list[str]]] = [
    # --- core mechanisms ---
    (
        "01-spot-orderbook",
        [
            "cryptocurrency spot trading order book bid ask spread maker taker fee",
            "--limit",
            "20",
            "--search-concurrency",
            "12",
            "--no-cache",
        ],
    ),
    (
        "02-spot-matching",
        [
            "crypto spot exchange matching engine limit market order execution",
            "--limit",
            "15",
            "--profile",
            "academic",
        ],
    ),
    (
        "03-spot-exact",
        [
            "spot trading cryptocurrency",
            "--mode",
            "exact",
            "--limit",
            "15",
        ],
    ),
    (
        "04-spot-venues",
        [
            "spot trading BTC USDT",
            "--site",
            "binance.com",
            "--site",
            "kraken.com",
            "--site",
            "coinbase.com",
            "--site",
            "investopedia.com",
            "--site",
            "bis.org",
            "--limit",
            "20",
            "--max-query-variants",
            "3",
            "--search-concurrency",
            "12",
        ],
    ),
    (
        "05-perp-funding",
        [
            "crypto perpetual futures funding rate mark price index price basis",
            "--limit",
            "25",
            "--search-concurrency",
            "12",
        ],
    ),
    (
        "06-cme-vs-perp",
        [
            "CME bitcoin futures versus crypto perpetual swap",
            "--limit",
            "15",
            "--profile",
            "academic",
        ],
    ),
    (
        "07-futures-venues",
        [
            "perpetual contract funding",
            "--site",
            "binance.com",
            "--site",
            "bybit.com",
            "--site",
            "deribit.com",
            "--site",
            "cmegroup.com",
            "--site",
            "investopedia.com",
            "--limit",
            "20",
            "--max-query-variants",
            "2",
        ],
    ),
    (
        "08-margin-liq",
        [
            "crypto margin trading isolated vs cross margin liquidation maintenance margin",
            "--limit",
            "25",
        ],
    ),
    (
        "09-margin-borrow",
        [
            "crypto margin borrowing interest rate loan-to-value",
            "--limit",
            "15",
        ],
    ),
    (
        "10-liq-cascade-news",
        [
            "crypto liquidation cascade long short squeeze perpetual futures",
            "--category",
            "news",
            "--time-limit",
            "w",
            "--region",
            "us-en",
            "--limit",
            "20",
        ],
    ),
    (
        "11-cftc-reg",
        [
            "CFTC cryptocurrency derivatives regulation perpetual futures",
            "--site",
            "cftc.gov",
            "--site",
            "sec.gov",
            "--site",
            "bis.org",
            "--site",
            "federalregister.gov",
            "--site",
            "esma.europa.eu",
            "--profile",
            "academic",
            "--limit",
            "20",
            "--max-query-variants",
            "2",
        ],
    ),
    (
        "12-mica",
        [
            "MiCA EU crypto derivatives trading regulation",
            "--region",
            "de-de",
            "--limit",
            "15",
            "--time-limit",
            "y",
        ],
    ),
    (
        "13-oss-bots",
        [
            "crypto futures trading bot perpetual",
            "--mode",
            "oss",
            "--github",
            "--site",
            "github.com",
            "--site",
            "gitlab.com",
            "--max-query-variants",
            "2",
            "--profile",
            "dev",
            "--limit",
            "20",
            "--search-concurrency",
            "12",
        ],
    ),
    (
        "14-oss-ccxt",
        [
            "ccxt unified crypto exchange trading API spot futures",
            "--mode",
            "oss",
            "--github",
            "--profile",
            "dev",
            "--limit",
            "20",
        ],
    ),
    (
        "15-news-funding",
        [
            "bitcoin perpetual funding rate negative",
            "--category",
            "news",
            "--time-limit",
            "m",
            "--region",
            "us-en",
            "--limit",
            "15",
            "--safe-search",
            "off",
        ],
    ),
    (
        "16-results-only",
        [
            "crypto spot vs futures vs margin differences",
            "--results-only",
            "--limit",
            "10",
        ],
    ),
    (
        "17-limit-100",
        [
            "cryptocurrency derivatives trading",
            "--limit",
            "100",
            "--search-concurrency",
            "16",
            "--no-cache",
        ],
    ),
    (
        "18-region-jp",
        [
            "暗号資産 先物 証拠金取引",
            "--region",
            "jp-jp",
            "--limit",
            "10",
        ],
    ),
    (
        "19-time-day",
        [
            "bitcoin futures",
            "--category",
            "news",
            "--time-limit",
            "d",
            "--limit",
            "10",
        ],
    ),
    (
        "20-academic-var",
        [
            "cryptocurrency futures margin liquidation risk value at risk",
            "--profile",
            "academic",
            "--limit",
            "20",
        ],
    ),
]


def run_job(job_id: str, args: list[str]) -> dict:
    stdout_path = RAW / f"{job_id}.json"
    stderr_path = RAW / f"{job_id}.err"
    started = time.time()
    proc = subprocess.run(
        ["uvx", "josty", *args],
        capture_output=True,
        text=True,
        timeout=180,
    )
    elapsed = time.time() - started
    stderr_path.write_text(proc.stderr or "")
    stdout_path.write_text(proc.stdout or "")
    meta = {
        "id": job_id,
        "args": args,
        "returncode": proc.returncode,
        "elapsed_s": round(elapsed, 3),
        "stdout_bytes": len(proc.stdout or ""),
        "stderr_bytes": len(proc.stderr or ""),
    }
    try:
        payload = json.loads(proc.stdout or "null")
        if isinstance(payload, dict):
            meta["status"] = payload.get("status")
            meta["schema_version"] = payload.get("schema_version")
            meta["partial"] = payload.get("partial")
            meta["cached"] = payload.get("cached")
            meta["result_count"] = len(payload.get("results") or [])
            meta["providers"] = [
                {
                    "name": p.get("name") or p.get("backend") or p.get("id"),
                    "ok": p.get("ok"),
                    "error": p.get("error"),
                    "error_kind": p.get("error_kind"),
                    "result_count": p.get("result_count"),
                }
                for p in (payload.get("providers") or [])
            ]
        elif isinstance(payload, list):
            meta["status"] = "results-only"
            meta["result_count"] = len(payload)
    except json.JSONDecodeError:
        meta["parse_error"] = True
        meta["stdout_head"] = (proc.stdout or "")[:400]
    return meta


def main() -> int:
    # Sequential batches of 4 to reduce breaker trips while still stressing concurrency flags.
    metas = []
    batch_size = 4
    for i in range(0, len(JOBS), batch_size):
        batch = JOBS[i : i + batch_size]
        with ThreadPoolExecutor(max_workers=batch_size) as pool:
            futs = {pool.submit(run_job, jid, args): jid for jid, args in batch}
            for fut in as_completed(futs):
                metas.append(fut.result())
                print(json.dumps({k: metas[-1][k] for k in ("id", "returncode", "elapsed_s", "status", "result_count") if k in metas[-1]}), flush=True)
    # Limit-push: sixth --site must fail
    started = time.time()
    proc = subprocess.run(
        [
            "uvx",
            "josty",
            "crypto",
            "--site",
            "a.com",
            "--site",
            "b.com",
            "--site",
            "c.com",
            "--site",
            "d.com",
            "--site",
            "e.com",
            "--site",
            "f.com",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    (RAW / "21-six-sites.json").write_text(proc.stdout or "")
    (RAW / "21-six-sites.err").write_text(proc.stderr or "")
    metas.append(
        {
            "id": "21-six-sites",
            "returncode": proc.returncode,
            "elapsed_s": round(time.time() - started, 3),
            "stderr_head": (proc.stderr or "")[:500],
            "stdout_head": (proc.stdout or "")[:300],
        }
    )
    print(json.dumps({"id": "21-six-sites", "returncode": proc.returncode}), flush=True)

    (ROOT / "campaign_meta.json").write_text(json.dumps(metas, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
