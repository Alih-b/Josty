# Issue #60: raw ddgs and Josty selected different engines

Status: partial investigation. Issue #60 remains open pending the broader
query-set, network, and TLS checks listed below.

The original raw-ddgs success does not establish that Google returned results.
With installed **ddgs 9.15.0**, Google ships with `disabled = True` and is absent
from `ddgs.engines.ENGINES["text"]`. Asking for `backend="google"` before importing
Josty falls back to `auto`. Josty deliberately re-registers the shipped Google
class at import time, so its Google branch actually queries Google.

This explains why the original standalone raw diagnostic could return results
while the Josty Google branch was empty. It is an engine-selection confound,
not evidence of a Josty concurrency regression. The original diagnostic kept
counts but not selected engines or HTTP destinations; its seven results cannot
be attributed retrospectively to a specific fallback engine.

## Reproduce engine selection without a search request

Use the project's installed environment, in a fresh Python process:

```python
from ddgs import DDGS
from ddgs.engines import ENGINES

print("google enabled:", "google" in ENGINES["text"])
print("raw selection:", [e.name for e in DDGS()._get_engines("text", "google")])

import josty  # registers the shipped Google class

print("google enabled:", "google" in ENGINES["text"])
print("Josty selection:", [e.name for e in DDGS()._get_engines("text", "google")])
```

On ddgs 9.15.0, the first selection contains Wikipedia, Grokipedia, Mojeek,
DuckDuckGo, Yahoo, Startpage, and Brave (order may vary); the second contains
only Google. `_get_engines` is a private diagnostic hook, not a supported API.

The relevant installed sources are `ddgs/engines/google.py` (`disabled = True`)
and `ddgs/ddgs.py` (`_get_engines` falls back to `auto` when no instances match).
Josty's corresponding registration is in `.agents/skills/josty/src/josty/engine.py`.

Version scope: this fallback is specific to ddgs 9.15.0. `pyproject.toml` allows
`ddgs>=9.15.0,<10`, and on an installation with ddgs 9.16.0 the same snippet prints
`google enabled: True` and `raw selection: ['google']`, because 9.16.0 no longer ships
Google disabled. Re-run the snippet on the version under test before reusing this
finding; the selection confound explains the 9.15.0 captures in
`60-google-probe.jsonl` only.

## Live comparison

`scripts/diagnose_google.py` is an opt-in live diagnostic, not a unit test. It
runs four arms in randomized order, each in a fresh
process to isolate the import-time registration:

1. Pristine raw ddgs, requesting `google`.
2. Raw ddgs after importing Josty, requesting the registered Google engine.
3. Josty's default engines with `max_search_concurrency=1`.
4. Josty's default engines with `max_search_concurrency=6`.

All arms use a fresh client, timeout 8 seconds, limit 10, moderate safe search,
default region, and no Josty cache. JSONL records selected engines, HTTP hosts
and status codes, exceptions, latency, and per-engine Josty outcomes. No response
bodies or credentials are recorded. Child failures remain visible as
`harness_error`; they must not be counted as empty search results.

```bash
python scripts/diagnose_google.py --repeats 5 > google-probe.jsonl
# Supply additional query strings to repeat the comparison on a larger set:
python scripts/diagnose_google.py --repeats 5 \
  "Python 3.13 release notes whatsnew" "PostgreSQL 17 release notes" > google-probe.jsonl
```

## Interpretation and scope

Local run on 2026-09-19, 00:28:25–00:30:53 UTC, Python 3.10, ddgs 9.15.0,
Josty 0.5.2 working tree based on `96f6fe2` with the #58 status fix:
five randomized repeats, seed 60, query `Python 3.13 release notes whatsnew`.
The [20 raw observations](60-google-probe.jsonl) contain no harness errors.

| Arm | Non-empty calls | Result counts by repeat | Google HTTP outcomes |
|---|---:|---|---|
| Pristine raw ddgs | 5/5 | 10, 7, 7, 7, 7 (fallback results) | No Google requests |
| Raw after Josty import | 0/5 | 0, 0, 0, 0, 0 | Five HTTP 200 responses |
| Josty sequential | 0/5 Google branches | 0, 0, 0, 0, 0 | Four HTTP 200 responses, one network exception |
| Josty fanout | 0/5 Google branches | 0, 0, 0, 0, 0 | Five HTTP 200 responses |

All pristine raw selections excluded Google; all registered raw selections
contained only Google. Both Josty arms selected Google for the Google branch.
Thus all five raw-success/Josty-empty comparisons were explained by different
engine selection. With engine selection matched, no raw-success/Josty-empty
pair was observed. The HTTP 200 Google responses still yielded no extracted
results in both raw and Josty calls.

This run used the available local network, not a separately verified clean
network: no Josty call had two non-empty engines. It covers one diagnostic query,
not the original twelve-query corpus. It establishes the selection confound
and does not establish why Google's upstream extraction is empty or exclude
all possible concurrency effects on other networks. No production Google,
fanout, retry, or client-reuse behavior was changed for #60.

A raw success is comparable only if the engine trace confirms Google was
selected. HTTP 200 alone does not establish successful search extraction.
Changes to production concurrency, client reuse, or retries are not justified
by the original comparison. The diagnostic preserves the evidence needed to
distinguish an actual Google discrepancy from an upstream fallback on future
ddgs versions and networks.

## Remaining work for #60

- Repeat the matched-engine comparison on the original twelve-query set, at
  least five repeats per query with randomized arm order.
- Repeat on a network where at least two engines return results, recording
  transport conditions separately from engine selection.
- Investigate the observed `SSL: WRONG_VERSION_NUMBER` errors. The current
  capture records those errors but does not establish their cause.
- Reassess Google extraction and concurrency using those results before closing
  the issue. The selection finding invalidates the old comparison; it does not
  prove the absence of a Josty-side issue in every environment.
