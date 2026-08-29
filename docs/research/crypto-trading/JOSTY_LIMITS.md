# Josty behavior from this campaign

Evidence is the JSON envelopes under `raw/` (`schema_version: "1.0"` unless the job is a CLI `ValueError`). Confidence is not uniform. Isolated, reproduced claims sit in **Confirmed**. Single-run or confounded observations sit in **Not findings**.

Harness:

- `run_josty_campaign.py` — concurrent flag sweep (batches of 4)
- `run_josty_isolation.py` — sequential cause isolation
- `campaign_meta.json` / `isolation_meta.json` — `rc` / `status` / `n` / `cached` / `t`

## Confirmed (isolated, reproducible)

These are one-flag or one-contract tests with a distinct exit or a repeated envelope field.

### CLI rejects over-bound inputs (exit 2, JSON on stderr)

| Job | Input | `rc` | stderr |
| --- | --- | --- | --- |
| `21-six-sites` | sixth `--site` | 2 | `{"error": "at most 5 site filters are allowed"}` |
| `39-limit-101` | `--limit 101` | 2 | `{"error": "limit must be between 1 and 100"}` |

Stdout is empty. This is not `status=complete` / `n=0`. The engine does not silently truncate.

Five `--site` filters succeed (`04-spot-venues`, `07-futures-venues`, `11-cftc-reg`).

### `--limit 100` is accepted; fused `count` is not 100

`--limit 100` is a valid CLI value. It is **not** a promise that `count==100`.

Do **not** cite `17-limit-100.json` for this. That job used query `cryptocurrency derivatives trading` and returned `status=complete`, `n=0`, all provider groups `error_kind=empty`. That is an empty branch, not a ceiling demonstration.

Cite the same live query as the recovery search, requested 100, `--no-cache`:

| Job | query | `n` | `cached` |
| --- | --- | --- | --- |
| `24-retry-limit100` | `crypto futures trading` | 16 | false |
| `40-limit-100-a` | same | 20 | false |
| `40-limit-100-b` | same | 16 | false |

All three are `status=complete` with `n` in 16–20, far below 100. Repeating the job does not pin a fixed fused size; it does show fused size ≪ requested 100 when backends return rows.

### `complete`+empty ≠ `degraded`

| Job | `status` | `n` | What it is |
| --- | --- | --- | --- |
| `09-margin-borrow` | complete | 0 | all groups `ok` + `error_kind=empty` |
| `17-limit-100` | complete | 0 | same (long query; **not** a `--limit` ceiling) |
| `03-spot-exact` | degraded | 15 | `partial=true`; one Google group `error_kind=network` (`ConnectError` / unreachable); other groups still returned rows |

`--results-only` (`16-results-only.json`) is a bare JSON array: no `schema_version`, no `status`. That is the documented shape, not a broken envelope.

Empty `complete` is **per invocation**, not a stable property of a query string. The `02-spot-matching` string was `n=0` in the campaign and `n=7` twice in isolation (`41-empty-a`, `41-empty-b`). Treat `n=0` as “this call’s backends returned empty,” not “this query is empty.”

### Diagnose `ok` is HTTP reachability

`00-diagnose-*.json`: every listed host `ok=true`, including Startpage `http_status=303`. Skill contract: that is not a search-quality signal.

### Disk cache works when isolated; `--no-cache` does not populate it

Engine write rule (source): cache a run only if `status != failed` **and** `len(results) > 0`. `--no-cache` sets `enable_cache=False`, so that process neither reads nor writes.

Isolation sequence after `--clear-cache` (`42`), unique query:

| Job | flags | `cached` | `n` | `t` (s) |
| --- | --- | --- | --- | --- |
| `43-cache-miss` | default | false | 8 | 1.182 |
| `44-cache-hit-a` | default | **true** | 8 | 0.378 |
| `45-cache-nocache` | `--no-cache` | false | 8 | 0.792 |
| `46-cache-hit-b` | default | **true** | 8 | 0.374 |

Hit latency is ~3× the miss in this pair and repeats on the second hit.

Confound reconstruction (why `31-bis-cached` is not a cache finding):

| Job | flags | `cached` | `n` | `t` (s) |
| --- | --- | --- | --- | --- |
| `47-confound-nocache` | `--no-cache` | false | 7 | 1.089 |
| `48-confound-second` | default | false | 8 | 1.370 |
| `49-confound-third` | default | **true** | 8 | 0.379 |

Call 1 does not write. Call 2 is a live miss (then writes if `n>0`). Call 3 hits. Campaign job `30` used `--no-cache`; `31` was therefore a live search that happened to `degraded`/`n=0`. That sequence cannot show a cache defect.

`--clear-cache` (`34`, `42`) returns `{"status": "cleared", ...}` and is not a `SearchRun`.

### Fetch extraction is bounded; JS/CSS pages fail closed

Not a search-backend bug. `--fetch` uses bounded download + text extract, no browser.

| Observation | Evidence |
| --- | --- |
| JS/CAPTCHA challenge text (~157 chars) | Binance academy in `29-fetch-spot`; some venue URLs in `35-fetch-margin2` |
| CME spec HTML collapses to CSS tokens | `28-fetch-cme` |
| Clean extract truncated at `--max-content-chars` | SoFi / Alphapoint / ADL pages in `26`, `35`, `36` (6000) |
| Fetch query can be `complete`/`n=0` | `27-fetch-margin`; agent reissued `35-fetch-margin2` (`n=7`) |

### `--mode oss` / `--github` / news / region / profile

Exercised and returned envelopes: `13-oss-bots`, `14-oss-ccxt`, `10-liq-cascade-news`, `15-news-funding`, `18-region-jp`, `12-mica` (`de-de` + `time-limit y`), `20-academic-var`. These confirm the flags are wired. They are not extra hard limits.

## Intended agent behavior (not a Josty deficiency)

Josty does **not** rewrite queries or retry backends when a branch is empty (`SKILL.md`: hidden amplification is worse than `degraded` / empty `complete`). The campaign’s shorter follow-ups (`22`, `23`, `24`, `35`) are **new CLI invocations** issued by the agent. That is the documented usage pattern, not compensation for a missing retry loop.

## Not findings (wrong citation, confounded, or unreproduced)

| Claim that would be too strong | Why it is not a finding | What to cite instead |
| --- | --- | --- |
| “`--limit 100` returned 0, so the 100-cap is weak” | `17-limit-100` is empty-query `complete` | `24`, `40-limit-100-a`, `40-limit-100-b` |
| “`31-bis-cached` shows cache miss / cache broken” | Prior job `--no-cache` (no write); `31` is live `degraded`/`n=0` | `43`–`46` and `47`–`49` |
| “Query X is empty” | Same string as `02` later returned `n=7` twice | Per-envelope `n` only |
| Fused `n` is always 16 at `--limit 100` | Repeats were 20 then 16 | Range 16–20 under this query |

`33-freqtrade-oss` (`degraded`, `n=15`) is a single-run network/partial observation, same class as `03-spot-exact`: useful as an example of `degraded` with remaining rows, not a named-backend SLA.

## Inventory (flags touched)

| Surface | Jobs | Note |
| --- | --- | --- |
| `--diagnose` | `00-*` | HTTP ok including 303 |
| `--limit` 10–100, 101 | many; `39` | 101 is hard fail; 100 is accepted |
| `--site` ×5 / ×6 | `04`/`07`/`11`; `21` | 6 is hard fail |
| `--mode` plain/exact/oss | `03`, `13`, `14` | |
| `--category news` + `--time-limit` | `10`, `15`, `19` | |
| `--region` | `10`, `12`, `18` | |
| `--safe-search off` | `15` | |
| `--profile` | `02`, `13`, `20`, `30` | |
| `--fetch` + fetch concurrency + `--max-content-chars` | `26`–`29`, `35`–`37` | |
| `--github` | `13`, `14`, `33`, `38` | |
| `--results-only` | `16` | array, no envelope |
| cache / `--no-cache` / `--clear-cache` | `30`–`31` confounded; `42`–`49` isolated | |
| `--max-query-variants` | `04`, `07`, `11`, `13` | |
| `--search-concurrency` 12–16 | `01`, `17`, `24`, `40-*` | |

## What this campaign does not show

Upstream engines throttle, return empty groups, and block fetches. No CAPTCHA, robots, paywall, or provider-control bypass. A ranked URL is not proof the page supports a claim. `cached: true` is a prior live envelope, not a fresh probe.
