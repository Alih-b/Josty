# Hand-off: the --health probe was giving false negatives

**Status:** partially fixed, needs independent verification
**Component:** Josty (src/josty/health.py, branch.py, cli.py)
**Found:** 2026-10-04
**Author:** previous agent in this checkout
**Read first:** AGENTS.md (invariants 1-6), then src/josty/health.py

---

## 1. The finding in one paragraph

Josty's backend health probe (--health) fetched each engine's search page with
**plain httpx**, whose TLS/HTTP2 fingerprint is not a browser's. Engines that run
bot management answered the probe with a challenge page, the classifier called
that "blocked", and the verdict was **written into a snapshot that the search
path reads**. The result was a confident, reproducible, and wrong conclusion:
"the network blocks these engines". The same requests through **primp with Chrome
impersonation** -- the transport ddgs actually uses -- returned full results pages
from the same machine and the same IP. The probe was measuring its own
fingerprint and reporting it as backend health.

This matters beyond the probe: a diagnostic artifact was feeding real search
status. Anything the probe writes is treated as evidence by branch.py.

---

## 2. How it was discovered

1. A 100-iteration research run recorded 5 of 6 backends as
   ok=true / error_kind="empty" with only yahoo returning results. That was first
   read as "the backends are fragile".
2. --health was added to distinguish a blocked engine from a quiet one.
3. First --health runs reported: brave=rate_limited(429), mojeek=challenged,
   startpage=challenged, google=js_required, duckduckgo+yahoo=ok. Conclusion drawn
   (wrongly): the egress IP is blocked.
4. The user contradicted it: Brave and Startpage load fine in their browser, on the
   same IP, with traffic tunnelled (Windscribe). No bot check.
5. Re-tests, in order of what they ruled out:
   - **User-Agent: not the cause.** Full Chrome header set vs the josty UA returned
     byte-identical challenge pages (mojeek 5505 bytes both times).
   - **Headers/host: not the cause.** All six hosts reachable (--diagnose).
   - **Headless Chrome: invalid test.** It was blocked too, which pointed at the IP
     -- but chrome --headless=new advertises "HeadlessChrome" in its UA and is
     fingerprint-detectable, so it proved nothing about the user's browser. Do not
     repeat this mistake: verify the UA a test client actually sends.
   - **IP: not the cause.** primp impersonating Chrome pulled a 221-243 KB Brave
     results page (21-91 "snippet" markers) from the same host.
6. Root cause confirmed: probe transport (httpx) vs search transport (primp).

## 3. Evidence (commands and observed results)

~~~bash
# 1. The probe's own answer (WRONG as a statement about the network)
.venv/bin/josty --health
# brave  rate_limited 429   challenge page
# mojeek challenged        title "Captcha", 5505 bytes
# startpage challenged     title "Startpage Blocked", 86370 bytes

# 2. Same URL, impersonating transport (what ddgs uses)
.venv/bin/python - <<'PY'
import primp
r = primp.Client(impersonate="chrome").get(
    "https://search.brave.com/search?q=python+packaging", timeout=25)
print(r.status_code, len(r.text), r.text.count("snippet-title"))
PY
# 200 217888 21      <-- real results, same IP

# 3. Headers are irrelevant
#    httpx with josty UA vs full Chrome headers -> identical bytes (5505 mojeek)

# 4. The mismatch propagates: the snapshot then makes the SEARCH path say blocked
XDG_CACHE_HOME=<writable> .venv/bin/josty --health
XDG_CACHE_HOME=<writable> .venv/bin/josty "python packaging" --no-cache
# before snapshot: brave ok=true error_kind=empty
# after  snapshot: brave ok=false error_kind=rate_limited  <-- probe artifact
~~~

Raw scripts and captured JSON from the investigation are in .local/scratch/:
probe_backends.py, probe_cause.py, classify_serps.py, three_clients.py,
probe_primp.py, parse_check.py, netprobe.sh, health*.json, srch*.json.

## 4. What was changed already (verify, do not assume)

- **src/josty/health.py** -- the default fetcher now uses primp with
  impersonate="chrome" (lazy import, falls back to httpx if primp is absent).
  httpx alone is the bug; keep primp.
- **src/josty/health.py** -- classifier rewritten:
  - challenge is detected from the page **title** (captcha, just a moment,
    access denied, forbidden, blocked) and from challenge-specific markup
    (g-recaptcha, hcaptcha, cf-challenge, ...);
  - a bare "captcha" substring in the body is NOT evidence (Brave's real results
    page contains the word);
  - result markup must repeat (>= 3 hits) to mean "ok", because a challenge page
    ships one copy of the engine's chrome;
  - a page with >= 8 outbound links and no challenge is treated as answered
    ("generic"), so a stale parser is not reported as a wall.
- **tests/test_health.py, tests/test_health_snapshot.py** -- 18 hermetic tests.
- **.agents/skills/josty/SKILL.md, AGENTS.md** -- documented the flag and the trap.

Current live verdict after the fix:

~~~
brave       ok           200  242983  marker x91: snippet
duckduckgo  ok           200   31497  marker x10: result__a
yahoo       ok           200  144773  marker x8: algo-sr
mojeek      challenged   200    5508  title: captcha
startpage   empty        200   22088  (page arrives, nothing parsable)
google      js_required  200   92408  noscript
~~~

Suite: 474 passed, 4 xfailed. ruff check . clean.

## 5. Open problems for you

**P1 (high) -- the probe still feeds search status.** branch.py calls
known_error_kind(provider) for any zero-result branch and, on a hit, reports
ok=false with that error_kind and records a breaker failure. A probe artifact
therefore changes real search semantics: status, partial, and backoff. Decide
whether that coupling should exist at all. Options: keep it but only ever
downgrade when the probe and the search path agree; or drop the coupling and let
--health stay purely advisory.

**P2 (high) -- snapshot staleness is one-way.** TTL is 24h. A backend that
recovers stays reported as blocked for up to a day, and a fresh failed probe can
flip a working backend to ok=false. Consider: shorter TTL, probe-per-run with a
cap, or "never upgrade, only annotate".

**P3 (medium) -- thresholds are tuned on six engines.** RESULT_MARKER_MIN_HITS=3
and RESULT_SHAPED_MIN_LINKS=8 were chosen by hand. Build a fixture corpus
(synthetic or saved pages) and pin the classifier against it, including:
a real-results page containing the word "captcha" (must be ok); a captcha-title
page; a JS shell; a 429; a 403; a page that is genuinely empty.

**P4 (medium) -- Brave is rate-flaky.** An immediate repeat fetch of the same URL
returned the 73 KB challenge page instead of the 243 KB results page. Any test
that hammers one engine will produce a false negative. Space out probes, and
never conclude "blocked" from a single request.

**P5 (medium) -- ddgs parsers are the next wall.** The search path returns nothing
from Brave even when the page is present:
~~~
ddgs brave parser expects: //div[@data-type='web']  and  div.snippet / div.content
on the fetched page:       0 occurrences of both
ddgs.text("python packaging", backend="brave") -> DDGSException: No results found.
~~~
Startpage is similar. So Josty's live coverage is duckduckgo+yahoo for parsing
reasons, not network reasons. Decide: add a small Josty-native extractor for
Brave/Startpage, or wait on upstream ddgs. If you add one, keep it in a module
with its own tests; do not grow engine knowledge inside engine.py.

**P6 (low) -- proxy support.** ddgs reads DDGS_PROXY (verified in
ddgs/ddgs.py:53). The health probe and fetch.py use httpx, which reads
HTTP(S)_PROXY. There is no --proxy flag. If a user needs a different egress, today
they must set both environment variables.

**P7 (low) -- --diagnose has the same transport flaw.** diagnose_run probes HTTPS
homepage reachability with httpx. Homepage reachability is a weaker signal, but
the fingerprint caveat applies there too.

**P8 (low) -- --health reports "empty" for a page that arrived.** startpage is
22 KB, no challenge, no markers, 1 outbound link. That is likely a JS shell. If
you can distinguish "answered but JS-only" from "answered with nothing", do it.

## 6. Reproduction recipe

~~~bash
cd /home/zerobyte/Josty
export UV_CACHE_DIR=$PWD/.local/uv-cache          # uv cache outside the workspace is read-only
export XDG_CACHE_HOME=$PWD/.local/xdg             # ~/.cache is read-only in this sandbox

python -m pytest -q                                # 474 passed, 4 xfailed
ruff check .

# the divergence that defines the bug: same URL, two transports
.venv/bin/python -c "import httpx; r=httpx.get('https://search.brave.com/search?q=x', timeout=20); print('httpx', r.status_code, len(r.text))"
.venv/bin/python -c "import primp; r=primp.Client(impersonate='chrome').get('https://search.brave.com/search?q=x', timeout=20); print('primp', r.status_code, len(r.text))"

.venv/bin/josty --health | python3 -m json.tool | head -40
~~~

## 7. Acceptance criteria for this hand-off

1. An independent reproduction of the false negative: httpx probe vs primp probe
   on the same URL, at the same moment, showing divergence. Record both.
2. A written decision on P1 and P2 (coupling and staleness), with tests that pin
   the chosen behaviour. If the coupling is kept, there must be a test proving a
   healthy probe cannot make a working backend report as failed.
3. A fixture-based classifier test set that includes a results page containing the
   word "captcha" (P3). No test may touch the network (invariant 2).
4. No new network calls inside research_run (invariant 4): the probe stays opt-in.
5. pytest -q, ruff check ., and the live smoke all pass:
   .venv/bin/josty "python packaging" | python3 -m json.tool > /dev/null

## 8. Cautions

- Do not hammer a single engine while testing; Brave rate-limits and will hand you
  a challenge page, which is how the original false negative was manufactured.
- primp prints certificate warnings in this sandbox ("failed to load native root
  certificate ... Permission denied" on /etc/ssl/certs). They are harmless here and
  do not indicate a TLS failure.
- The sandbox blocks writes outside the workspace; point XDG_CACHE_HOME and
  UV_CACHE_DIR inside the checkout.
- Nothing here is committed. src/josty/health.py and the two test files are
  untracked; newspaper/ is untracked. Do not commit without asking.
- Prior agents in this session twice shipped conclusions that had to be retracted
  (IP-blocked backends; and an earlier "5 of 6 backends are fragile" claim). Treat
  every verdict in this document as a hypothesis until you reproduce it yourself.
