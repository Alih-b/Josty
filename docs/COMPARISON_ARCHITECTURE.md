# Architectural comparison: josty vs raw_ddgs vs websearch-skill

This document contrasts the internal architecture of **josty**,
`raw_ddgs`, and `websearch-skill`. It explains why `josty` achieves
faster execution, cleaner deduplication, and a smaller footprint.

## Summary

| Dimension | `josty` | `raw_ddgs` | `websearch-skill` |
|---|---|---|---|
| **Architecture** | Async parallel fanout with RRF fusion | Serial blocking requests | Multi-engine subprocess tool |
| **Dependencies** | 3 direct (`ddgs`, `httpx`, `trafilatura`) | 1 direct (`ddgs`) | Multi-package environment |
| **Output** | Strict, versioned JSON contract | Raw unnormalized dicts | JSON / text |

---

## Data-flow diagram

```text
                       QUERY: "Rust foundation"
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
        ▼                     ▼                     ▼
   raw ddgs                Josty              websearch-skill
 (no wrapper)          (async wrapper)        (multi-layer skill)
        │                     │                     │
        │   ┌─────────────────┤                     │
        │   │ Query expansion │                     │
        │   │ plain / exact / │                     │
        │   │ oss + site:     │                     │
        │   └─────────────────┤                     │
        │                     │                     │
        │   ┌─────────────────┤   ┌─────────────────┤
        │   │ Backend fanout  │   │ Router selects  │
        │   │ 3 groups in     │   │ adapters        │
        │   │ parallel        │   │ (ddgs default)  │
        │   └─────────────────┤   └─────────────────┤
        │                     │                     │
        ▼                     ▼                     ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│ ddgs.text()   │   │ ddgs.text()   │   │ ddgs_adapter  │
│ once per      │   │ once per      │   │               │
│ backend,      │   │ group, under  │   │ EngineAdapter │
│ in series     │   │ semaphore     │   │ abstraction   │
└───────┬───────┘   └───────┬───────┘   └───────┬───────┘
        │                     │                     │
        │   ┌─────────────────┤                     │
        │   │ Merge query     │                     │
        │   │ variants per    │                     │
        │   │ backend         │                     │
        │   └─────────────────┤                     │
        │                     │                     │
        │   ┌─────────────────┤   ┌─────────────────┤
        │   │ RRF fusion,     │   │ Correlation-    │
        │   │ k=60, one pass  │   │ group fusion    │
        │   │                 │   │ (provenance +   │
        │   │                 │   │ weighted votes) │
        │   └─────────────────┤   └─────────────────┤
        │                     │                     │
        │   ┌─────────────────┤   ┌─────────────────┤
        │   │ canonical()     │   │ canonical()     │
        │   │ dedup + strip   │   │ dedup + strip   │
        │   │ tracking params │   │ tracking params │
        │   └─────────────────┤   └─────────────────┤
        │                     │                     │
        ▼                     ▼                     ▼
   raw list                 Josty               websearch-skill
   (all rows,            result list             result list
    unordered,           (10 items,              (10 items,
    duplicates)          scored, provenance)     scored, provenance)
        │                     │                     │
        │   ┌─────────────────┤                     │
        │   │ status:         │                     │
        │   │ complete /      │                     │
        │   │ degraded /      │                     │
        │   │ failed          │                     │
        │   └─────────────────┤                     │
        │                     │                     │
        ▼                     ▼                     ▼
   caller gets          caller gets             caller gets
   ~20-40 unranked      JSON envelope with      Envelope with
   duplicate rows       schema_version,         contract_version,
                        providers[],           providers[] (some
                        partial flag           engines hard-fail)
```

### What each box costs or buys

| Box | raw ddgs | Josty | websearch-skill |
|---|---|---|---|
| Query expansion | absent | `expand()` adds exact/oss/site variants | absent at search layer; query rewriting is caller-side |
| Backend fanout | serial, one call per backend | parallel groups under semaphore | parallel adapters, ddgs adapter uses `auto` backend |
| Fusion | none | single RRF pass | multi-stage correlation-group fusion |
| Dedup | none | `canonical()` strips tracking params | `canonical()` + correlation handling |
| Status / provenance | none | `ProviderStatus`, `SearchRun.status` | `Envelope.ok`, provider list |
| Output contract | none | versioned JSON object | versioned Envelope object |

---

## Why Josty wins on speed

### Mechanism

- **raw ddgs** calls each backend in series because the caller loop is
  synchronous. If you ask for 8 backends, you pay the sum of 8
  round-trips plus whichever ones time out.
- **Josty** uses `asyncio.gather` over the backend groups with a
  shared semaphore. The wall clock is dominated by the slowest group,
  not the sum.
- **websearch-skill** is also async internally, but it routes through
  more abstraction layers and its ddgs adapter has a 10s default
  timeout. That extra margin shows up in the tail.

### v3 numbers

| runner | mean latency (successful runs) |
|---|---:|
| Josty | **1.62 s** |
| websearch-skill | 3.22 s |
| raw ddgs | 5.48 s |

The v3 Wilcoxon test on successful pairs reports Josty faster
than raw_ddgs (p < 0.0001) and faster than websearch-skill
(p < 0.0001). The difference is not sampling noise — it follows
from the serial-vs-parallel fanout design.

---

## Why Josty wins on deduplication and result cleanliness

### Mechanism

- **raw ddgs** returns exactly what each backend returns. The same
  page often appears from Bing, Google, and DuckDuckGo with different
  tracking query strings, so the caller receives 2–4 copies of the
  same URL.
- **Josty** normalizes every URL through `canonical()`, strips
  `utm_*` and ad-tracking params, and fuses duplicates into a single
  scored result. The fused result keeps backend-group provenance (`sources: ["bing,brave,duckduckgo", "google,mojeek,startpage"]`) and the best snippet.
- **websearch-skill** also deduplicates and tracks provenance, but
  its fusion is more complex (correlation groups, weighted votes). On
  simple factual queries that complexity does not improve the top-10
  slice.

### v3 numbers

| runner | nDCG@10 (successful runs) | MRR (successful runs) |
|---|---:|---:|
| Josty | **0.960** | 1.000 |
| websearch-skill | 0.954 | 0.988 |
| raw ddgs | 0.870 | 0.989 |

The difference vs raw_ddgs is statistically significant (p < 0.0001).
The difference vs websearch-skill is **not** significant (p ≈ 0.70).
So deduplication/RRF buys a large gap over no wrapper, but only a
small, non-significant gap over the rival wrapper.

---

## Why Josty can lose on reliability

### Mechanism

- **raw ddgs** swallows per-backend failures: if backend #3 times out,
  the loop continues and returns whatever backends #1-2 produced.
  From the caller's perspective there is no failure state, just a
  shorter list.
- **Josty** uses a fixed backend pool (`DEFAULT_BACKENDS`) and
  reports `status: degraded` when any group fails. If the only group
  that would have produced results fails, the final fused list can be
  empty even though other runners returned rows.
- **websearch-skill** adapter layer catches engine failures and returns
  an `EngineOutput(error=...)`; the router then fuses whatever
  succeeded. In practice it behaves more like raw_ddgs (return partials)
  than like Josty (report degraded).

### v3 numbers

| runner | empty-run rate (all runs) |
|---|---:|
| Josty | **51%** (70/136) |
| websearch-skill | 40% (55/136) |
| raw ddgs | 35% (48/136) |

This is the most important honest finding. In v2, websearch-skill
looked unreliable because its ddgs adapter hard-failed from one IP.
In v3, with backend rotation, **Josty became the least reliable**
because its wider pool means more chances for a throttled engine to
cause a degraded/empty run. The reliability ranking is a function of
how each runner treats partial failure, not just engine quality.

---

## Why Josty wins on footprint and auditability

### Mechanism

- **raw ddgs** is not a tool; it is a dependency. The "footprint" is
  whatever script the user writes around it.
- **Josty** is ~1,350 lines of Python, 3 direct deps (28 resolved packages in closure), one CLI.
  Every backend call, every dedup decision, and every failure is in
  one package and visible in the JSON output.
- **websearch-skill** is ~15k+ lines across layers (search, extract,
  format, agentio, tools, store, contracts). It can do far more
  (MCP, page store, quality scoring, arXiv/GitHub tools) but you
  cannot audit it in an afternoon.

### Why it matters

For an agent runtime that wants to spawn a search subprocess, review
it for security, and bound its behavior, Josty's smaller surface
is the point. websearch-skill is the better choice when you need the
full stack. The benchmark numbers alone do not capture this; the
source-tree size and dependency closure do.

---

## Trade-off summary

```text
Dimension           raw ddgs    Josty          websearch-skill
──────────────────────────────────────────────────────────────
Speed               slow        fastest        fast
Dedup / ranking     none        strong         strong
Result quality      weakest     tied-for-best  tied-for-best
Reliability         highest     lowest         middle
Footprint           script-sized small         large
Features            minimal     minimal        rich
Auditability        ad-hoc      high           moderate
Failure visibility  none        explicit       explicit
```

"Reliability" here means "likelihood of returning at least one result
when engines throttle." Josty's lower reliability is a direct
consequence of its explicit failure model: it would rather tell the
caller the run was degraded than silently return a thin result set.

---

## The bottom line

- Choose **raw ddgs** when you want to write your own wrapper or when
  you do not care about duplicates and ranking.
- Choose **Josty** when you need a small, fast, auditable
  subprocess that deduplicates, fuses, and reports failures honestly.
  It is the best on speed and the smallest footprint; its quality
  ties the rival wrapper; its main cost is a higher empty-run rate
  under heavy throttling.
- Choose **websearch-skill** when you need MCP, a page store, SearXNG,
  extraction quality scoring, or other full-stack features. It is not
  meaningfully better than Josty on the narrow search task the
  benchmark measures, but it is built for a broader task.

The benchmark supports this conclusion, not the reverse. Josty's
niche is real, bounded, and honestly described.