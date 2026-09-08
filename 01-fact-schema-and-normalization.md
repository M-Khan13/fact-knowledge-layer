# Fact schema + normalization rules

This is the intellectual core of the system. Own it. Everything downstream (matching,
reconciliation, the four cases) inherits the shape you define here. Hand the
*implementation* to Claude Code, but these rules are yours.

---

## 1. The fact object

Every extracted fact is one row with this shape:

| Field | Meaning | Example |
|---|---|---|
| `fact_id` | unique id | `f_00214` |
| `collection_id` | which knowledge layer it belongs to | `delhivery` |
| `subject` | the entity the fact is about | `Delhivery Limited` |
| `subject_key` | canonical id for the subject (see §4) | `CIN:L63090DL2011PLC221234` |
| `attribute` | free-text, NOT an enum | `revenue_from_operations` |
| `value_raw` | value exactly as written | `81,415.38` |
| `value_num` | parsed number in a base unit | `81415380000` (rupees) |
| `unit` | resolved unit | `INR` (base) / raw `INR_million` |
| `context` | the context signature (see §3) | `{period, scope, basis, vintage}` |
| `source_doc` | file it came from | `02-delhivery-annual-report-fy24` |
| `page` | page in that doc | `22` |
| `evidence_span` | the verbatim sentence/quote | `"...Revenue from Operations 81,415.38..."` |
| `confidence` | 0-1, how sure the extractor is | `0.82` |

**Key design decision:** `attribute` is free text, not a fixed list. A new kind of fact
(e.g. `forex_reserves`, `director_board_status`) just appears as a new attribute string.
That single choice gives you the **dynamic-schema brownie point** for free.

---

## 2. Why a "context signature" (this is your differentiator)

Most people normalize units + dates and stop. That produces false contradictions
(₹8,142 Cr "vs" 81,415 million; FY revenue "vs" Q4 revenue).

Instead, split every fact into **value** + **context signature**. The context signature is
a small structured tag that says *under what conditions the value is true*. Comparison then
becomes a two-stage gate:

1. **Same `subject_key` + same `attribute`?** → these two facts are about the same thing.
2. **Same context signature?**
   - Same context, values agree (within tolerance) → **corroborate**
   - Same context, values disagree → **contradict**
   - Different context → **reconcilable-by-context**, and the *reason* is literally which
     context field differs (period / scope / basis / vintage).

The verdict is decided **deterministically** by comparing context fields *before* any LLM
adjudication. The LLM only writes the human-readable reason and handles fuzzy cases. This is
what makes your reasoning explainable and non-hallucinated — and it's the exact "guardrails
around LLM behaviour" mindset Superjoin says they hire for.

---

## 3. Normalization rules (deliberately not the generic ones)

### 3a. Unit rule — "unit-blind comparison is banned"
- Every monetary value is stored as `value_num` in a single base (plain rupees) **plus** its
  original unit. `₹ Cr` → ×10⁷, `₹ million`/`₹ mn` → ×10⁶, `₹ lakh` → ×10⁵, `₹ bn` → ×10⁹.
- **Hard rule:** two values are *never* compared until both carry a resolved unit. A fact with
  a missing/ambiguous unit gets `confidence` capped low and is marked `no-verdict`, never
  auto-contradicted. (This kills the ₹8,142 Cr vs 81,415 mn trap.)
- Percentages, ratios, counts (shipments in Mn, headcount) each get their own unit family;
  never cross-compare families.

### 3b. Temporal rule — Indian fiscal awareness + subset logic
- Parse into `{period_type, fiscal_year, sub_period}` where `period_type ∈
  {FY, H, Q, CY, month, point_in_time}`.
- **Indian FY mapping:** `FY24` = 1 Apr 2023 – 31 Mar 2024. Map IMF's `FY2024/25` and RBI's
  `2024-25` to the **same** `fiscal_year=2025` token. (Different publishers, same period —
  this is what lets macro facts match at all.)
- **Subset logic:** if one period is contained in the other (`Q4 FY24 ⊂ FY24`,
  `H1 FY25 ⊂ FY25`), a value difference is **reconcilable (part-of-whole)**, never a
  contradiction.

### 3c. Scope / basis rule
- Tag `scope ∈ {consolidated, standalone}` and `basis` (domain-specific): for company
  financials `{revenue_from_operations, revenue_from_services, total_income}`; for macro
  `{GDP_market_price, GVA, real, nominal}`.
- Same subject+attribute but different scope/basis → **reconcilable**, reason = the differing
  tag (e.g. "standalone vs consolidated").

### 3d. Vintage rule — revisions are not contradictions
- Tag `vintage ∈ {advance_estimate, provisional, revised, final, projection}`.
- Same period, same measure, different value across vintages → **reconcilable (revision)**,
  and the reason names which figure is newer. (Handles Survey's 6.4% first-advance-estimate
  vs later 6.5% actuals.)
- Precedence for "which is newer": `advance_estimate < provisional < revised < final`.

### 3e. Entity / alias rule — use strong keys
- People: resolve on **DIN** (Director Identification Number) when present — it's a stable
  government key, stronger than name-matching. `S. Barua` / `Sahil Barua` → same DIN.
- Companies: resolve on **CIN** when present, else normalized legal name
  (`Delhivery Limited` = `the Company` = `your Company`).
- Addresses: normalize whitespace/case/abbreviations before deciding two addresses are the
  same place.

### 3f. Corroboration tolerance band
- Two same-context numeric facts "agree" if within the **larger of** ±0.5% relative or one
  unit in the last reported decimal. Inside band → corroborate; outside → contradict.
- Percentages (macro): treat ±0.1 percentage point as agreement (rounding/measure noise).

---

## 4. What the verdict engine outputs per pair

```
{
  fact_a, fact_b,
  verdict: corroborate | contradict | reconcilable | no-verdict,
  reason_code: same_value | value_conflict | period_subset | period_diff |
               scope_diff | basis_diff | unit_diff_resolved | vintage_revision |
               unit_missing | unit_incomparable | context_unstated |
               same_category | category_conflict,
  reason_text: "<one human line the LLM writes>",
  confidence
}
```

Reason codes in full:

| Code | Verdict | Meaning |
|---|---|---|
| `same_value` | corroborate | same context, values agree within the band |
| `unit_diff_resolved` | corroborate | agree once both units resolve to one base (₹ Cr vs ₹ mn) |
| `value_conflict` | contradict | same context, values outside the band |
| `period_subset` | reconcilable | one period contains the other (Q4 FY24 ⊂ FY24) |
| `period_diff` | reconcilable | different periods that do **not** nest (FY24 vs FY25) |
| `scope_diff` | reconcilable | standalone vs consolidated |
| `basis_diff` | reconcilable | a different measure (GVA vs GDP, real vs nominal) |
| `vintage_revision` | reconcilable | same period at a different stage of revision |
| `unit_missing` | no-verdict | one side states no unit that resolves |
| `unit_incomparable` | no-verdict | both units resolve but do not convert (% vs ₹, INR vs USD) |
| `context_unstated` | no-verdict | values differ, but one side leaves a condition unstated that could account for it |
| `same_category` | corroborate | two categorical facts report the same state |
| `category_conflict` | contradict | two categorical facts report opposite states (on the board vs resigned) |

Facts split into two kinds. A fact whose value reads as a quantity is a
**measurement**, compared by magnitude within the unit rules above. A fact
whose value does not read as a number is **categorical** - a status, a role, a
date - and is compared by category, since there is no magnitude to weigh. A
measurement against a category is neither, and reaches `unit_missing`. A bare
number with no unit stays a measurement, so the unit-blind ban still holds for
it: "740" is never treated as a category.

`period_diff` and `unit_incomparable` exist because the two cases they name are
real and were otherwise being mislabelled. A non-nested period difference is
still reconcilable per §2, but calling it `period_subset` would assert a
containment that does not hold. And a missing unit is a gap in the source
document, whereas two well-stated units that simply do not convert is a
category error — the same verdict, but a different thing to go and fix.

Where several context fields differ, `period` decides the reason code: a figure
covering a different span of time is a different figure whatever else also
changed. Every differing field is recorded alongside the code.

`reason_code` is deterministic (from the context comparison); `reason_text` is the LLM's
plain-English gloss. Show both in the UI — the code proves the logic, the text explains it.
