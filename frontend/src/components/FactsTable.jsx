import {
  formatAttribute,
  formatDocument,
  formatPage,
  formatPeriod,
  formatValue,
} from "../format.js";

const COLUMNS = [
  { key: "subject", label: "Subject" },
  { key: "attribute", label: "Attribute" },
  { key: "value", label: "Value" },
  { key: "period", label: "Period" },
  { key: "source", label: "Source" },
  { key: "action", label: "" },
];

export default function FactsTable({ facts, onViewSource }) {
  if (!facts.length) {
    return (
      <div className="panel">
        <div className="state">No facts match that filter.</div>
      </div>
    );
  }

  return (
    <div className="panel">
      <div className="facts-scroll">
        <table className="facts">
          <colgroup>
            {COLUMNS.map((column) => (
              <col key={column.key} className={`col--${column.key}`} />
            ))}
          </colgroup>
          <thead>
            <tr>
              {COLUMNS.map((column) => (
                <th key={column.key} scope="col">
                  {column.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {facts.map((fact) => (
              <tr key={fact.fact_id}>
                <td className="facts__subject">{fact.subject}</td>
                <td className="facts__attribute">{formatAttribute(fact.attribute)}</td>
                <td className="facts__value">{formatValue(fact)}</td>
                <td className="facts__period">{formatPeriod(fact)}</td>
                <td className="facts__source" title={fact.source_doc}>
                  {formatDocument(fact.source_doc)}
                  {fact.page == null ? "" : ` · ${formatPage(fact)}`}
                </td>
                <td className="facts__action">
                  <button
                    type="button"
                    className="btn-ghost"
                    onClick={() => onViewSource(fact.fact_id)}
                    disabled={fact.page == null}
                    title={
                      fact.page == null
                        ? "This fact is not grounded to a page"
                        : undefined
                    }
                  >
                    View source
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
