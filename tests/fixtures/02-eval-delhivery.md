# Eval set — Delhivery collection

**How to use this:** open each cited PDF to the given page and confirm the fact is really
there. Page = **PDF page index within the excerpt** (not the printed page). If a fact checks
out, it's a true positive your engine should reproduce. The four cases at the bottom are what
you'll show on video.

Docs:
- `01-delhivery-prospectus-2022-excerpt.pdf` (P)
- `02-delhivery-annual-report-fy24-excerpt.pdf` (AR)
- `03-delhivery-q4-fy24-earnings-presentation.pdf` (Deck)

---

## A. Facts to verify (spot-check list)

| # | Subject · attribute | Value | Context | Source | Page |
|---|---|---|---|---|---|
| 1 | Delhivery · revenue from operations | ₹81,415.38 mn (= ₹8,141.5 Cr) | FY24, consolidated | AR | 22 |
| 2 | Delhivery · revenue from operations | ₹74,540.82 mn (= ₹7,454.1 Cr) | FY24, standalone | AR | 22 |
| 3 | Delhivery · revenue from operations | ₹72,253.01 mn | FY23, consolidated | AR | 22 |
| 4 | Delhivery · revenue from services | ₹8,142 Cr | FY24, consolidated | Deck | 6, 9, 17 |
| 5 | Delhivery · revenue from services | ₹2,076 Cr | Q4 FY24 | Deck | 17 |
| 6 | Delhivery · total income | ₹8,594 Cr | FY24 | Deck | 17 |
| 7 | Delhivery · total income | ₹49,114.06 mn (= ₹4,911 Cr) | FY21, restated | P | 5, 17 |
| 8 | Delhivery · Express Parcel shipments | 740 Mn | FY24 | Deck | 9 |
| 9 | Suvir Suren Sujan · board status | Non-Exec Nominee Director, since 7 Mar 2019 | as of 2022 | P | 30, 85 |
| 10 | Suvir Suren Sujan · board status | resigned w.e.f. 24 Aug 2023 | FY24 | AR | 33, 91 |
| 11 | Sahil Barua · role | Managing Director & CEO | FY24 | AR | 24 |

> Note on #1 vs #4: AR calls it "revenue from operations", the Deck calls it "revenue from
> services". They're ₹8,141.5 Cr vs ₹8,142 Cr — the same figure to rounding. The tiny gap is
> because services revenue excludes ~₹2 Cr of traded-goods revenue (Deck p.17). Good detail to
> mention as nuance.

---

## B. The four required cases

### Case 1 — Corroboration (expressed differently)
- **Fact 1** (AR p.22): FY24 consolidated revenue = **₹81,415.38 million**
- **Fact 4** (Deck p.6): FY24 revenue from services = **₹8,142 Cr**
- **Expected verdict:** corroborate.
- **Why it's non-trivial:** different unit (₹ million vs ₹ Cr) *and* different label
  ("operations" vs "services"). Only after unit normalization (÷10 to get Cr) and entity match
  do they line up. A naive string/number match would miss this entirely.
- **Reason line to show:** "Same FY24 consolidated figure once units are aligned
  (₹81,415.38 mn = ₹8,141.5 Cr ≈ ₹8,142 Cr); labels differ but refer to the same revenue."

### Case 2 — Genuine / likely contradiction
- **Fact 9** (P p.30, p.85): Suvir Suren Sujan listed as a **sitting** Non-Executive Nominee
  Director, "since March 7, 2019."
- **Fact 10** (AR p.33, p.91): Suvir Suren Sujan **resigned** from the Board w.e.f. 24 Aug 2023.
- **Expected verdict:** flagged as a conflict in board status for the same person (same DIN).
- **Note:** this one is *genuine as stated* (the two documents assert opposite board statuses),
  but it is fully **explained by time** — which is exactly why it also powers Case 3's logic.
  For Case 2, present it as "same director, opposite status across documents → conflict raised."
- **Reason line:** "Prospectus (2022) presents Sujan as an active director; FY24 report records
  his resignation — a genuine status conflict between the two filings."

### Case 3 — Apparent contradiction, reconciled by context (THE money case)
Pick whichever of these reads cleanest on video; #3a is the strongest.

- **3a — reconciled by PERIOD:** Fact 4 (FY24, ₹8,142 Cr) vs Fact 5 (Q4 FY24, ₹2,076 Cr).
  Numbers differ ~4×. **Reconcilable:** Q4 ⊂ FY24 (part-of-whole). Not a contradiction.
  - Reason: "Values differ because one covers full FY24 and the other only Q4 FY24 — a quarter
    is a subset of the year, so this is expected, not a conflict."
- **3b — reconciled by SCOPE:** Fact 1 (consolidated, ₹8,141.5 Cr) vs Fact 2 (standalone,
  ₹7,454.1 Cr), same FY24. **Reconcilable:** consolidated vs standalone.
  - Reason: "Same period; the gap is standalone vs consolidated scope, not a disagreement."
- **3c — reconciled by TIME (the director):** treat Case 2 through the time lens — active in
  2022, resigned in 2023; both true at their respective dates once you attach time context.

### Case 4 — An extraction / reasoning failure you found
Two real ones surfaced while mining — either works; **#4a is the cleanest to demo:**

- **4a — table row-bleed mis-associates a DIN.** In the prospectus director table, my text
  extraction produced `DIN: 02442753 Suvir Suren Sujan` on p.85, while p.30 shows Sujan's DIN
  as `01173669`. The 02442753 actually belongs to the *previous* row's director — the table
  layout bled one row's DIN onto the next name. **Handling:** trust DIN only when it sits on the
  same text line as the name; when a person shows two DINs across pages, flag `low-confidence`
  and don't emit a false "same person, conflicting DIN" contradiction. Fix path: parse the
  director table by columns (pdfplumber table extraction) instead of flat text.
- **4b — scale-word blindness.** The AR revenue line reads `74,540.82 66,586.61 81,415.38
  72,253.01` (four values across scope×year columns, in ₹ million). Flat text extraction can
  grab `81,415.38` with no unit and no column header, which would look like it contradicts the
  Deck's `8,142`. **Handling:** the unit-blind-comparison ban (§3a) forces `no-verdict` on any
  unit-less number rather than a false contradiction; proper fix is header-aware table parsing
  so each value keeps its (scope, FY, unit) tags.

---

## C. Extra pairs you can show if you want more depth
- Fact 7 (FY21 total income, ₹4,911 Cr) vs Fact 6 (FY24 total income, ₹8,594 Cr) →
  reconcilable by period; also nicely shows the layer spanning 2019–2024 vintages.
- Fact 3 (FY23 consolidated) vs Fact 1 (FY24 consolidated) → reconcilable by period, and the
  YoY growth (~12.7%) matches the Deck's stated growth — a cross-doc consistency check.
