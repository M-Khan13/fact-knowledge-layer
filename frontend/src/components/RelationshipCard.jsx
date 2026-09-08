import {
  formatAttribute,
  formatDocument,
  formatPage,
  formatPeriod,
  formatValue,
  verdictOf,
} from "../format.js";

function Side({ fact, onViewSource }) {
  if (!fact) return <div className="rel__value">—</div>;

  return (
    <div>
      <div className="rel__value">{formatValue(fact)}</div>
      <button
        type="button"
        className="rel__source"
        onClick={() => onViewSource(fact.fact_id)}
        disabled={fact.page == null}
        title={fact.source_doc}
      >
        {formatDocument(fact.source_doc)}
        {fact.page == null ? "" : ` · ${formatPage(fact)}`}
      </button>
    </div>
  );
}

export default function RelationshipCard({ relationship, onViewSource }) {
  const verdict = verdictOf(relationship.verdict);
  const a = relationship.facts?.fact_a;
  const b = relationship.facts?.fact_b;

  // The pair share a subject and attribute by construction, so the caption
  // names them once; the period comes from whichever side states one, since a
  // period difference is itself a common reason for the verdict.
  const anchor = a || b;
  const period = formatPeriod(a || {}) !== "—" ? formatPeriod(a) : formatPeriod(b || {});

  return (
    <article className="rel">
      <header className="rel__head">
        <div className="rel__caption">
          {anchor ? (
            <>
              {anchor.subject} · {formatAttribute(anchor.attribute)}
              {period && period !== "—" ? ` · ${period}` : ""}
            </>
          ) : (
            relationship.relationship_id
          )}
        </div>
        <span className={`pill pill--${verdict.tone}`}>{verdict.label}</span>
      </header>

      <div className="rel__pair">
        <Side fact={a} onViewSource={onViewSource} />
        <div className="rel__operator" aria-hidden="true">
          {verdict.operator}
        </div>
        <Side fact={b} onViewSource={onViewSource} />
      </div>

      {relationship.reason_text ? (
        <p className="rel__reason">{relationship.reason_text}</p>
      ) : null}
    </article>
  );
}
