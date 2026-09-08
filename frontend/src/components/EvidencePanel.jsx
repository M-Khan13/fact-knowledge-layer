import { useEffect, useRef, useState } from "react";
import { evidenceSrc } from "../api.js";
import {
  formatAttribute,
  formatDocument,
  formatPeriod,
  formatValue,
} from "../format.js";

export default function EvidencePanel({ fact, onClose }) {
  // Keyed on the fact so moving between facts shows the spinner again rather
  // than the previous page while the new one renders.
  const [status, setStatus] = useState("loading");
  const closeRef = useRef(null);

  useEffect(() => setStatus("loading"), [fact.fact_id]);

  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    closeRef.current?.focus();

    // The workspace behind the panel keeps its scroll position; locking the
    // body stops the page underneath scrolling with the panel's own overflow.
    const { overflow } = document.body.style;
    document.body.style.overflow = "hidden";

    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = overflow;
    };
  }, [onClose]);

  const period = formatPeriod(fact);
  const page = fact.page_label ?? fact.page;

  return (
    <>
      <button
        type="button"
        className="scrim"
        aria-label="Close evidence"
        onClick={onClose}
      />

      <aside
        className="evidence"
        role="dialog"
        aria-modal="true"
        aria-label={`Evidence for ${fact.subject} ${formatAttribute(fact.attribute)}`}
      >
        <header className="evidence__head">
          <div className="evidence__eyebrow">Evidence</div>
          <h2 className="evidence__title">
            {fact.subject} · {formatAttribute(fact.attribute)}
          </h2>
          <div className="evidence__value">
            {formatValue(fact)}
            {period && period !== "—" ? <span> · {period}</span> : null}
          </div>
          <button
            type="button"
            className="evidence__close"
            onClick={onClose}
            aria-label="Close"
            ref={closeRef}
          >
            ×
          </button>
        </header>

        <div className="evidence__body">
          <div className="evidence__source">
            <span title={fact.source_doc}>{formatDocument(fact.source_doc)}</span>
            <span className="evidence__page">
              {page == null ? "Page unknown" : `Page ${page}`}
            </span>
          </div>

          <figure className="evidence__figure">
            {status === "error" ? (
              <div className="evidence__placeholder">
                The page image could not be rendered.
              </div>
            ) : (
              <>
                {status === "loading" ? (
                  <div className="evidence__placeholder">
                    <div className="spinner" />
                    Rendering page {page}…
                  </div>
                ) : null}
                <img
                  className="evidence__img"
                  src={evidenceSrc(fact)}
                  alt={`Page ${page} of ${formatDocument(fact.source_doc)}, with the supporting text boxed`}
                  style={status === "loading" ? { display: "none" } : undefined}
                  onLoad={() => setStatus("ready")}
                  onError={() => setStatus("error")}
                />
              </>
            )}
          </figure>

          <p className="evidence__caption">
            Extracted verbatim. The highlight marks the sentence this fact was
            grounded in.
          </p>
        </div>
      </aside>
    </>
  );
}
