# Crypto trading research: spot, futures, and margin

**Method:** live Josty metasearch (`schema_version: "1.0"`). Flag sweep: `run_josty_campaign.py`. Cause isolation: `run_josty_isolation.py`. Josty does not rewrite empty queries; follow-up CLI calls with shorter strings are the intended agent pattern (`SKILL.md`), not a workaround. Tool contracts and citation rules: `JOSTY_LIMITS.md`. URL catalog: `SOURCES.md`.

**How to read this:** **Observed** = stated in a Josty result title, snippet, or fetched body. **Inference** = synthesis across sources. Blogs, Reddit, and news wires are discovery, not proof. News hits were kept only when the subject token (liquidation, squeeze, funding, bitcoin/crypto) appears in title or snippet.

---

## 1. Spot trading

### What it is (observed)

Spot trading is buying or selling the asset for (near) immediate delivery against quote currency, typically via a central limit **order book**. Josty hits describe **maker** orders that rest in the book versus **taker** orders that cross the spread and pay higher fees (`01-spot-orderbook.json`; fetched maker/taker explainers in `29-fetch-spot.json`).

Fetched text (non-JS pages) states that makers add liquidity and takers remove it, and that fee schedules often rebate makers and charge takers (`29-fetch-spot.json`, “Understanding the Difference Between Taker Orders and Maker …”).

Venue docs appear under five-site filter `--site binance.com --site kraken.com --site coinbase.com --site investopedia.com --site bis.org` (`04-spot-venues.json`), mixing exchange help centers with general education.

### Market structure (inference + mixed sources)

A typical CEX spot stack: matching engine, order types (limit, market, stop), maker/taker fees, and an order book of bids/asks. One campaign call of a long matching-engine query was **empty complete** (`02-spot-matching.json`). Josty left that envelope as-is; a **new** shorter search (`22-retry-matching.json`) ranked OSS matching-engine repos (e.g. `github.com/ArjunVachhani/order-matcher`) — software discovery, not a venue spec. Re-running the original `02` string later returned `n=7` (`41-empty-a/b`), so that empty is not a stable query property.

**Not verified here:** any specific exchange’s matching-priority rules (price-time vs pro-rata) from primary matching-engine documentation; Binance Academy fetch was a JS/bot wall (`29-fetch-spot.json`).

### Fees and execution (observed)

Independent explainers and ScienceDirect bibliographic hits discuss maker/taker fee changes and order-book anatomy (`01-spot-orderbook.json`). Treat academic abstracts as pointers; the ScienceDirect hit is a paywalled landing page, not the paper body.

---

## 2. Futures (dated CME vs crypto perpetuals)

### Perpetual futures mechanics (observed from fetch)

Fetched perpetual explainers (`26-fetch-funding.json`) describe:

- **Perpetual** contracts with no expiry, kept near spot by a **funding rate** paid between longs and shorts.
- **Index price** from spot venues versus **mark price** used for PnL/liquidation (to reduce manipulation of last trade).
- Funding as a periodic cash flow; ignoring it is described as a common error for holders.

Coinbase Learn ranked in search but returned **no extractable content** on fetch (JS/empty). Messari similarly unfetched. CME academic PDF titles appeared without body (`26`).

**Inference:** funding sign tells you whether longs pay shorts (positive) or the reverse; crowding on one side can make holding expensive even if price is flat.

### CME Bitcoin futures (observed, weak fetch)

`--site cmegroup.com --fetch` (`28-fetch-cme.json`) ranked:

- [Bitcoin Futures Contract Specs](https://www.cmegroup.com/markets/cryptocurrencies/bitcoin/bitcoin/contractspecifications.html)
- “What are Bitcoin Futures?” explainer (best extract): **USD cash-settled** contract, ticker **BTC**, based on **CME CF Bitcoin Reference Rate (BRR)** aggregating major spot venues’ USD bitcoin trades.

Contract-spec HTML largely failed extraction (CSS crumbs). **Do not cite contract multipliers, tick sizes, or margin rates from this campaign** until a human opens CME specs or a non-JS source.

Comparison query “CME bitcoin futures versus crypto perpetual swap” (`06-cme-vs-perp.json`) is ranking evidence that the two products are commonly contrasted: listed, dated, cash-settled futures vs unexpiry perpetuals with funding.

### Perp DEX example (observed snippet, failed fetch)

`32-perp-dex.json` includes [Hyperliquid funding docs](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding). `--fetch` of a Hyperliquid query returned empty (`37-fetch-hl.json`). Treat GitBook as a primary candidate that Josty **discovered** but did not successfully extract in this run.

### Positioning news (observed titles; not causal proof)

Week-window news (`10-liq-cascade-news.json`) includes liquidation and squeeze language in titles, e.g. crypto futures liquidations, bitcoin open interest / short squeeze (Decrypt), large short books. Month-window funding news (`15-news-funding.json`) must still be checked token-by-token before citing rates. These are **market color**, not a model of funding.

---

## 3. Margin (spot margin and futures margin)

### Isolated vs cross (observed from fetch)

`--fetch` retry (`35-fetch-margin2.json`) extracted consistent definitions:

- **Cross margin:** collateral is **shared** across positions in an account (or coin wallet). One position’s loss can drain margin backing others.
- **Isolated margin:** collateral is **capped to one position**. Liquidation of that position does not automatically consume the rest of the account’s free balance (beyond what was assigned).

SoFi’s fetched page uses the same isolated/shared distinction in a broker-general way (`35`). Crypto-native blogs (Alphapoint, Bitunix) apply it to leveraged crypto. First fetch attempt `27-fetch-margin.json` was empty `complete`; the agent issued a new query (`35-fetch-margin2`) rather than expecting Josty to retry.

### Borrowing / interest (observed after retry)

Initial “loan-to-value” query was empty on that invocation (`09-margin-borrow.json`). A new, shorter search (`23-retry-borrow.json`) ranked academy-style pages on **borrowing, interest, and liquidation** (e.g. Blofin academy). **No primary exchange interest-rate table was fetched** in this campaign.

### Liquidation, insurance fund, ADL (observed from fetch)

`25-adl-insurance.json` discovered the loss waterfall. Fetched bodies (`36-fetch-adl.json`) state a repeated industry description:

1. Position hits **maintenance margin** → **liquidation** toward **bankruptcy price**.
2. If the close is worse than bankruptcy, **insurance fund** (pooled backstop) absorbs residual.
3. If the fund is insufficient, **auto-deleveraging (ADL)** reduces **opposing profitable** positions so the failed account does not leave uncleared loss.

**Inference:** ADL is a tail-risk socialized mechanism unlike a CCP guarantee fund on a listed future. **Not verified:** any named venue’s exact ADL ranking formula (profit, leverage, position size) from official docs in the fetched set.

---

## 4. Risk comparison (synthesis)

| Dimension | Spot | Dated futures (e.g. CME BTC, as described) | Crypto perps + margin |
| --- | --- | --- | --- |
| Exposure | Inventory / cash; no expiry | Contract until expiry; cash-settled BRR (CME explainer) | Synthetic price vs mark; funding |
| Leverage | Optional via spot margin | Exchange/clearing margins | Often high; isolated vs cross |
| Carry | None beyond opportunity/fees | Term basis vs spot | Funding payments |
| Blow-up path | Adverse price; borrowed spot margin | Margin call / listed liquidation | Mark-based liq → insurance → ADL |
| Counterparty | Exchange custody (CEX) | Clearinghouse model (listed) | CEX or DEX; insurance/ADL instead of CCP |

This table is **inference** from the sources above, not a regulator determination.

Academic-profile hits (`20-academic-var.json`, `30-bis-academic.json`) point at VaR/liquidation literature and BIS commentary on crypto exchange growth; Coindesk’s BIS-related headline is secondary journalism.

---

## 5. Regulation (US / EU pointers only)

Five-site regulator filter (`11-cftc-reg.json`: cftc.gov, sec.gov, bis.org, federalregister.gov, esma.europa.eu) ranked **primary-looking** URLs, including:

- SEC press and interpretive material on federal securities laws and crypto (`sec.gov` press releases and PDF `33-11412`).
- CFTC press/speeches (`cftc.gov` Release 9241-26; Selig statement URL).
- SEC–CFTC MOU PDF (`sec.gov/files/mou-sec-cftc-2026.pdf`).
- Dodd-Frank derivatives spotlight on SEC.

**These are discovery of official pages.** This campaign did **not** `--fetch` the PDFs (download/JS limits). Do not summarize legal holdings from titles alone.

MiCA / EU (`12-mica.json`, region `de-de`, `time-limit y`) ranked EU crypto-regulation explainers. Treat as a lead list, not a legal memo.

---

## 6. OSS and agent-usable tooling (observed)

`--mode oss --github --profile dev` (`13-oss-bots.json`, `14-oss-ccxt.json`, `33-freqtrade-oss.json`, `38-github-ccxt.json`):

- **ccxt** unified exchange API (spot and derivatives in the project’s own positioning): `github.com/ccxt/ccxt`, PyPI.
- **Freqtrade** ecosystem adapters (e.g. GMX+ccxt+freqtrade repo).
- Perp/futures bots: Passivbot, Bybit/Binance futures bots, GitHub topic `perpetual-futures`.

**Safety:** repositories are untrusted code. Discovery ≠ audit. Do not run third-party trading bots from search hits.

---

## 7. Conflicts and gaps

| Topic | Conflict / gap |
| --- | --- |
| CME numeric specs | Ranked but fetch unusable |
| Binance/OKX official education | JS challenge on fetch |
| Funding formulas | Explainers agree on “longs pay shorts when premium” qualitatively; **no single canonical formula** extracted from a named venue |
| Fused count vs `--limit 100` | Tool claim lives in `JOSTY_LIMITS.md`: cite `24` / `40-limit-100-*` (`n` 16–20), **not** `17-limit-100` (`n=0` empty branch) |
| Cache | Isolated hits in `44`/`46` (`cached: true`). `31-bis-cached` is confounded (prior `--no-cache` does not write) |
| Regulation URLs | Ranked official pages only; PDFs unfetched — catalog, not holdings |
| Japanese region query | `18-region-jp.json` returned 10 results; not used as legal/venue fact without translation QA |

---

## 8. Practical takeaways (labeled)

1. **Spot** is inventory and book microstructure (maker/taker). **Observed.**
2. **Perps** add mark/index and **funding**; PnL and liquidation typically follow mark, not last trade. **Observed in explainers.**
3. **Listed BTC futures (CME explainer)** are cash-settled to a reference rate, not a perpetual. **Observed in one fetched CME page.**
4. **Isolated vs cross** is a collateral ring-fence choice. **Observed in fetched pages.**
5. **Insurance fund + ADL** is the crypto-native loss waterfall after liquidation. **Observed in fetched explainers; venue formulas unverified.**
6. **Official US pages** exist in the result set for SEC/CFTC; **legal conclusions not drawn.**
7. **ccxt / freqtrade / perp bots** are the OSS cluster Josty actually returned. **Observed URLs only.**
