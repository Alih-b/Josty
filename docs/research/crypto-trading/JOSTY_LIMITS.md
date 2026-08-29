# Josty limit and capability campaign (crypto trading)

This file records what happened when Josty was driven across **every documented CLI surface** while researching crypto spot, futures, and margin markets. Authoritative evidence is the JSON envelopes under `raw/` (`schema_version: "1.0"` unless noted).

## Inventory exercised

| Surface | How it was used | Evidence |
| --- | --- | --- |
| `--diagnose` | text, news, github | `raw/00-diagnose-*.json` — hosts HTTP-ok including Startpage `303` |
| `--limit` 1–100 | 10, 15, 20, 25, 100 | Engine accepts 100; fused `count` often far below 100 (`24-retry-limit100.json` n=16) |
| `--site` ×5 (max) | venue + regulator filters | `04-spot-venues`, `07-futures-venues`, `11-cftc-reg` |
| `--site` ×6 | expected hard fail | `21-six-sites.err`: `at most 5 site filters are allowed`, exit 2 |
| `--mode plain/exact/oss` | default, exact phrase, OSS fanout | `03-spot-exact` (degraded), `13-oss-bots`, `14-oss-ccxt` |
| `--category news` | liquidations, funding, day window | `10-liq-cascade-news`, `15-news-funding`, `19-time-day` |
| `--region` | `us-en`, `de-de`, `jp-jp` | `10`, `12-mica`, `18-region-jp` |
| `--safe-search off` | news funding | `15-news-funding` |
| `--time-limit` d/w/m/y | day news, week liq, month funding, year MiCA | `19`, `10`, `15`, `12` |
| `--profile` general/dev/academic | matching, OSS, BIS, CFTC | `02`, `13`, `20`, `30` |
| `--fetch` + `--fetch-concurrency` | 6–8, `--max-content-chars` 5000–8000 | `26`, `28`, `29`, `35`, `36` |
| `--github` | OSS bots, ccxt, diagnose | `13`, `14`, `33`, `38`, `00-diagnose-github` |
| `--results-only` | spot vs futures vs margin | `16-results-only.json` is a JSON array, no envelope |
| `--no-cache` / cache / `--clear-cache` | live probes + clear | `34-clear-cache.json` `status: cleared`; `31-bis-cached` did **not** hit cache (degraded empty live call) |
| `--max-query-variants` | 1, 2, 3 | site-expanded venue/reg/OSS jobs |
| `--search-concurrency` | 12 and 16 | `01`, `17`/`24` |
| `--max-content-chars` | 5000–8000 | fetch jobs; SoFi/Alphapoint/ADL pages truncated at 6000 |

## Diagnose contract

`--diagnose` `ok=true` means the host answered HTTP, **including 303**. That is reachability, not search quality (`SKILL.md`). All listed search hosts plus `api.github.com` answered in this environment.

## Empty `complete` is not an outage

Per Josty rules, `status=complete` with `error_kind=empty` is a successful empty branch. Observed:

- `02-spot-matching.json` — long academic query, all three backend groups empty.
- `09-margin-borrow.json` — same pattern.
- `17-limit-100.json` — `--limit 100` + `--no-cache` + concurrency 16, **all groups empty**.
- Retries with shorter queries recovered: `22-retry-matching` n=7, `23-retry-borrow` n=17, `24-retry-limit100` n=16 (still not 100 fused hits).

Josty does not rewrite empty queries. The campaign issued new searches.

## Degradation and network

- `03-spot-exact.json`: `status=degraded`, `partial=true`. Google group: `ConnectError` / `Network is unreachable (os error 101)` (`error_kind=network`). Other groups still returned 15 results.
- `31-bis-cached.json`: live (non-cached) follow-up `degraded` with zero results after a `--no-cache` first hit — cache did not hold the prior envelope for reuse in this sequence.
- `33-freqtrade-oss.json`: `degraded` but still n=15 OSS/GitHub hits.

## Fetch limits (real pages)

| Observation | Evidence |
| --- | --- |
| JS/CAPTCHA pages yield ~157 chars of challenge text, not article body | Binance academy in `29-fetch-spot`; some OKX/Binance margin URLs in `35` |
| CME spec HTML often collapses to CSS tokens (`style style ,`) | `28-fetch-cme` contract-spec URLs |
| Explainer pages fetch cleanly up to the char cap | SoFi, Alphapoint, Bitunix, Zoomex/BeInCrypto-style funding, ADL explainers in `26`/`35`/`36` |
| `--fetch` empty envelope | `27-fetch-margin.json` n=0; retry `35-fetch-margin2` n=7 |
| Hyperliquid fetch query empty | `37-fetch-hl.json` n=0; **snippets** still found via `32-perp-dex` (`hyperliquid.gitbook.io`) |

## Hard engine bounds confirmed

- `limit must be between 1 and 100` (engine); CLI `--limit 100` accepted.
- `MAX_SITES = 5`; sixth `--site` is a CLI error JSON on stderr/stdout, not a search envelope.
- Circuit breakers exist in-process (3 failures / 60s → 30s cool-down). This campaign batched four concurrent `uvx` processes to reduce self-inflicted breaker opens; some backend groups still returned `empty` under load.

## What “full potential” does not mean

Josty is keyless metasearch. Upstream engines throttle, return empty groups, and block fetches. This campaign **did not** bypass CAPTCHAs, robots, or paywalls. Claims below are only as strong as snippets + bounded `--fetch` text.
