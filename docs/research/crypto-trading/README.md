# Crypto trading research pack (Josty)

Evidence-backed notes on **spot**, **futures/perps**, and **margin**, produced by driving Josty through its full CLI surface (diagnose, five `--site` filters, news/time/region, OSS+GitHub, fetch, concurrency, cache, results-only, and hard-fail sixth site).

| File | Role |
| --- | --- |
| [RESEARCH.md](RESEARCH.md) | Synthesis with observed vs inference labels |
| [JOSTY_LIMITS.md](JOSTY_LIMITS.md) | Capability and failure evidence |
| [SOURCES.md](SOURCES.md) | Per-job titles and URLs from envelopes |
| [run_josty_campaign.py](run_josty_campaign.py) | Reproducible multi-job runner |
| [raw/](raw/) | `schema_version: "1.0"` JSON + stderr |

Re-run (network required):

```bash
python3 docs/research/crypto-trading/run_josty_campaign.py
```
