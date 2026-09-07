# Eval set — India macroeconomy collection

**How to use this:** open each cited PDF to the given page and confirm. Page = **PDF page
index within the excerpt** (not the printed page). The four cases are what you'll show.

Docs:
- `01-india-economic-survey-2024-25-excerpt.pdf` (ES)
- `02-rbi-annual-report-2024-25-excerpt.pdf` (RBI)
- `03-imf-india-2025-article-iv-excerpt.pdf` (IMF)

Period note: ES `FY25`, RBI `2024-25`, IMF `FY2024/25` all mean the **same** April 2024 –
March 2025 year. Your temporal normalizer must map all three to one `fiscal_year=2025` token —
that mapping is what makes these facts comparable at all.

---

## A. Facts to verify (spot-check list)

| # | Subject · attribute | Value | Context | Source | Page |
|---|---|---|---|---|---|
| 1 | India · real GDP growth | 6.4% | FY25, advance estimate | ES | 4, 14 |
| 2 | India · real GDP growth | 6.5% | FY2024/25, at market prices | IMF | 3, 5, 10 |
| 3 | India · real GDP growth | 6.5% (Q1) | Q1 2024-25 | RBI | 24 |
| 4 | India · real GDP growth (H1) | 6.0% | H1 FY25 | ES | 20 |
| 5 | India · CPI headline inflation | 5.4% | FY24 | ES | 28 |
| 6 | India · CPI headline inflation | 4.9% | Apr–Dec 2024 (partial FY25) | ES | 28 |
| 7 | India · current account deficit | 0.6% of GDP | FY2024/25 | IMF | 12 |
| 8 | India · current account deficit | 0.7% of GDP | FY2023/24 | IMF | 12 |
| 9 | India · forex reserves | USD 640.3 bn | end-Dec 2024 | ES | 37 |

> IMF real-GDP row (p.5) reads across years: `... 9.2  6.5  6.6  6.2` — the `6.5` aligns with
> FY2024/25 per the text on p.3/p.10; `6.6` and `6.2` are projections for FY25/26 and FY26/27.

---

## B. The four required cases

### Case 1 — Corroboration (expressed differently)
- **Fact 2** (IMF p.10): India real GDP grew **6.5%** in FY2024/25.
- **Fact 3** (RBI p.24): real GDP rose **6.5%** in Q1 2024-25 (and RBI's full-year table aligns).
- **Expected verdict:** corroborate (both ≈6.5%, different publishers, different phrasing).
- **Why non-trivial:** the period tokens are written three different ways across institutions;
  only after mapping `FY2024/25` = `2024-25` = `FY25` do they match.
- **Reason line:** "IMF and RBI both report FY25 real GDP growth ≈6.5%; publishers and wording
  differ, the figure agrees."

### Case 2 — Genuine / likely contradiction
- **Fact 1** (ES p.14): FY25 real GDP growth = **6.4%**.
- **Fact 2** (IMF p.10): FY2024/25 real GDP growth = **6.5%**.
- Same period, same measure, values differ by 0.1pp. Whether this is a *contradiction* or a
  *reconcilable revision* depends on vintage — which is the whole point (see Case 3).
- **Use as Case 2 if** you present it before applying vintage logic: "Two institutions report
  different FY25 growth (6.4% vs 6.5%) for the same year → flagged as a potential conflict."
- **Reason line:** "Same year, same metric, different value (6.4% vs 6.5%) — raised as a likely
  conflict pending context."

### Case 3 — Apparent contradiction, reconciled by context (THE money case)
Two clean options; #3a is the strongest macro example.

- **3a — reconciled by VINTAGE (revision):** the same 6.4% vs 6.5% from Case 2. The Survey's
  6.4% is a **first advance estimate**; IMF/RBI's 6.5% is a later **actual/provisional** figure.
  Same period, different data vintage → **reconcilable revision**, not a conflict.
  - Reason: "The 0.1pp gap is a data-vintage difference — Survey's advance estimate (6.4%) vs
    later provisional actual (6.5%) — the newer figure supersedes; not a contradiction."
- **3b — reconciled by PERIOD:** Fact 4 (H1 FY25 = 6.0%) vs Fact 2 (full FY25 = 6.5%). Looks
  like a conflict; resolved because H1 ⊂ FY25 (a half-year is a subset of the year).
  - Reason: "6.0% covers only H1 FY25; 6.5% is the full year — part-of-whole, expected."
- **3c — inflation by period:** Fact 5 (5.4%, full FY24) vs Fact 6 (4.9%, Apr–Dec 2024).
  Different periods → reconcilable, and it correctly shows inflation *falling*, not a data clash.

### Case 4 — An extraction / reasoning failure you found
- **4a — multi-year table row flattening.** The IMF real-GDP row (p.5) is a sequence of
  year-columns: `9.7 7.6 9.2 6.5 6.6 6.2`. Flat text extraction loses the column headers, so an
  extractor can grab the wrong number for FY25 (e.g. pick 6.6, a *projection*, instead of 6.5).
  **Handling:** don't trust a value pulled from a numeric run without a resolved period header;
  cap confidence and require the header cell. Fix path: table-aware extraction that keeps each
  number bound to its year column and `vintage=projection|actual` tag.
- **4b — unit/measure conflation.** "Real GDP at market prices" (IMF) vs GVA-based figures
  elsewhere are *different measures*; comparing them as if identical produces spurious
  mismatches. **Handling:** the `basis` tag (§3c) keeps `GDP_market_price` and `GVA` in separate
  lanes so they're reconciled-by-basis, not contradicted.

---

## C. Extra pairs for depth
- Fact 7 (CAD 0.6% FY25) vs Fact 8 (CAD 0.7% FY24) → reconcilable by period; shows the deficit
  narrowing, a real trend rather than a clash.
- Fact 9 (forex USD 640.3 bn, end-Dec 2024) is a nice single grounded fact to demo click-to-
  evidence even before any relationship is formed.
