/**
 * Turning stored fields into something a reader can scan.
 *
 * The pipeline keeps the figure as the document printed it (`value_raw`) next
 * to what normalization made of it, and the UI shows the printed one — that is
 * the number a reader can check against the page image. The units need more
 * care than pasting them on the end, which is most of what is here.
 */

// A currency reads as a prefix; everything else trails the number.
const CURRENCIES = {
  INR: { label: "₹", aliases: ["₹", "inr", "rs", "rs.", "rupee", "rupees"] },
  USD: { label: "US$", aliases: ["$", "usd", "us$", "dollar", "dollars"] },
  EUR: { label: "€", aliases: ["€", "eur", "euro", "euros"] },
};

const SUFFIXES = {
  million: { label: "million", aliases: ["million", "mn", "mm"] },
  billion: { label: "billion", aliases: ["billion", "bn"] },
  trillion: { label: "trillion", aliases: ["trillion", "tn"] },
  thousand: { label: "thousand", aliases: ["thousand", "k"] },
  crore: { label: "crore", aliases: ["crore", "crores", "cr"] },
  lakh: { label: "lakh", aliases: ["lakh", "lakhs", "lac"] },
  percent: { label: "%", aliases: ["%", "percent", "per cent", "pc"] },
  percentage: { label: "percentage point", aliases: ["percentage point", "pp"] },
  point: { label: "point", aliases: ["point", "points"] },
  bps: { label: "bps", aliases: ["bps", "basis point", "basis points"] },
  years: { label: "years", aliases: ["year", "years", "yr", "yrs"] },
  months: { label: "months", aliases: ["month", "months"] },
  days: { label: "days", aliases: ["day", "days"] },
  tons: { label: "tons", aliases: ["ton", "tons", "tonne", "tonnes"] },
  tonnes: { label: "tonnes", aliases: ["tonne", "tonnes", "ton", "tons"] },
  MT: { label: "MT", aliases: ["mt", "metric ton", "metric tonnes"] },
  GW: { label: "GW", aliases: ["gw", "gigawatt", "gigawatts"] },
  MW: { label: "MW", aliases: ["mw", "megawatt", "megawatts"] },
  kg: { label: "kg", aliases: ["kg", "kilogram", "kilograms"] },
  // "count" is dimensionless — a headcount or a shipment tally carries no unit
  // of its own, so it never contributes a word.
  count: { label: "", aliases: [] },
  per: { label: "per", aliases: ["per", "/"] },
};

const escapeForRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/**
 * Whether the printed value already says this unit.
 *
 * Word boundaries matter more than they look: "cr" sits inside "increase" and
 * "mn" inside "amount", so a plain substring test would quietly drop the unit
 * off a value that never stated it. Symbols are matched literally, since \b
 * does not apply to a rupee sign.
 */
function alreadyStated(raw, alias) {
  const lowered = raw.toLowerCase();
  if (!/^[a-z0-9. ]+$/.test(alias)) return lowered.includes(alias);
  return new RegExp(`(^|[^a-z])${escapeForRegExp(alias)}([^a-z]|$)`, "i").test(raw);
}

const stated = (raw, aliases) => aliases.some((alias) => alreadyStated(raw, alias));

/**
 * The value as a reader should see it.
 *
 * A fact the pipeline could not resolve a unit for (`comparable: false`) is a
 * category, not a measurement — its `unit_canonical` holds a slug of the value
 * itself rather than a unit, so it is deliberately ignored and only the raw
 * text is shown.
 *
 * For a measurement, the unit tokens are appended only where the document did
 * not already print them: "10.94%" stays as it is instead of becoming
 * "10.94% percent", while a bare "28,367.97" gains the "million" it needs.
 */
export function formatValue(fact) {
  // A value lifted out of a PDF table can carry the line break it was printed
  // across ("6.9 \nper cent"); it is one value, so it is shown on one line.
  const raw = String(fact.value_raw ?? "").replace(/\s+/g, " ").trim();
  if (!raw) return "—";
  if (!fact.comparable || !fact.unit) return raw;

  let prefix = "";
  const trailing = [];

  for (const token of String(fact.unit).split("_")) {
    if (!token) continue;

    const currency = CURRENCIES[token.toUpperCase()];
    if (currency) {
      if (!stated(raw, currency.aliases)) prefix = currency.label;
      continue;
    }

    const suffix =
      SUFFIXES[token] ||
      SUFFIXES[token.toLowerCase()] ||
      // An unrecognised token is still worth showing; assume it reads as
      // written rather than dropping information off the value.
      { label: token.replace(/_/g, " "), aliases: [token.toLowerCase()] };

    if (suffix.label && !stated(raw, suffix.aliases)) trailing.push(suffix.label);
  }

  // "%" hugs the number; words do not.
  const tail = trailing.reduce(
    (text, part) => (part === "%" ? `${text}%` : `${text} ${part}`),
    "",
  );
  return `${prefix}${raw}${tail}`;
}

/** `litigation_aggregate_amount_involved` reads better as a phrase. */
export function formatAttribute(attribute) {
  if (!attribute) return "—";
  const words = String(attribute).replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * The period as the document worded it, falling back to what normalization
 * resolved when the page left it implicit.
 */
export function formatPeriod(fact) {
  const written = fact.context?.period;
  if (written) return written;
  const resolved = fact.signature?.period;
  if (!resolved) return "—";
  if (resolved.raw) return resolved.raw;
  if (resolved.fiscal_year) {
    const type = resolved.period_type === "FY" ? "FY" : "";
    return `${type}${resolved.fiscal_year}${resolved.sub_period ? ` ${resolved.sub_period}` : ""}`;
  }
  return "—";
}

/**
 * Filenames are the only document names the corpus has, so they are tidied
 * for display — the ordering prefix dropped, hyphens opened out — while the
 * stored name stays available as a tooltip.
 */
export function formatDocument(sourceDoc) {
  if (!sourceDoc) return "Unknown document";
  const words = String(sourceDoc)
    .replace(/^\d+[-_]/, "")
    .replace(/[-_]+/g, " ")
    .trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * A collection's name, which in this corpus is its id — tidied the same way a
 * document name is, so "india-macroeconomy" reads as a title.
 */
export const formatCollection = (collection) =>
  formatDocument(collection?.name || collection?.collection_id || "");

/**
 * Where a fact sits in its document. `page_label` is what the PDF itself
 * declares and is null across this corpus, so the physical page is what shows;
 * it is preferred when a document does carry labels.
 */
export function formatPage(fact) {
  if (fact.page_label) return `p. ${fact.page_label}`;
  return fact.page == null ? "" : `p. ${fact.page}`;
}

/**
 * The four verdicts reconciliation can reach, with the glyph that sits between
 * the two values and the tone its pill is drawn in.
 */
export const VERDICTS = {
  contradict: { label: "Contradiction", tone: "contradict", operator: "≠" },
  corroborate: { label: "Corroborated", tone: "corroborate", operator: "=" },
  reconcilable: { label: "Reconciled", tone: "reconcile", operator: "≈" },
  "no-verdict": { label: "No verdict", tone: "neutral", operator: "·" },
};

export const verdictOf = (verdict) =>
  VERDICTS[verdict] || { label: formatAttribute(verdict), tone: "neutral", operator: "·" };

export const plural = (count, noun, suffix = "s") =>
  `${count.toLocaleString()} ${noun}${count === 1 ? "" : suffix}`;
