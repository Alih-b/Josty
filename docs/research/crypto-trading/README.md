# Crypto trading research pack (Josty)

Josty flag sweep plus sequential isolation jobs. Tool behavior and claim-to-envelope mapping live in `JOSTY_LIMITS.md`. Domain notes in `RESEARCH.md` are labeled observed vs inference and are not the audit surface for Josty contracts.

| File | Role |
| --- | --- |
| [JOSTY_LIMITS.md](JOSTY_LIMITS.md) | Confirmed vs not-findings; cites the job that actually demonstrates each claim |
| [run_log.md](run_log.md) | `rc` / `status` / `n` / `cached` / `t` for campaign + isolation |
| [RESEARCH.md](RESEARCH.md) | Domain synthesis (weaker than the tool log; titles/fetches, not PDFs) |
| [SOURCES.md](SOURCES.md) | Per-job titles and URLs |
| [run_josty_campaign.py](run_josty_campaign.py) | Concurrent flag sweep |
| [run_josty_isolation.py](run_josty_isolation.py) | Sequential cache / limit / empty isolation |
| [raw/](raw/) | Envelopes + stderr for CLI rejects |

Re-run (network required):

```bash
python3 docs/research/crypto-trading/run_josty_campaign.py
python3 docs/research/crypto-trading/run_josty_isolation.py
```
