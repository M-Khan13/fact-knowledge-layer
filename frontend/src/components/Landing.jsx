import { Link } from "react-router-dom";
import NavBar from "./NavBar.jsx";
import useResource from "../useResource.js";
import { listCollections } from "../api.js";
import { formatCollection, plural } from "../format.js";

function Collections() {
  const { data, error, loading } = useResource((options) => listCollections(options), []);

  if (loading) {
    return (
      <div className="entry__grid">
        <div className="shimmer" style={{ height: 54 }} />
        <div className="shimmer" style={{ height: 54 }} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="state state--error">
        {error.message}
        <div className="state__hint">Start the API, then reload this page.</div>
      </div>
    );
  }

  if (!data?.length) {
    return (
      <div className="state">
        No collections yet.
        <div className="state__hint">
          Ingest a document with <code>scripts/ingest.py</code> to create one.
        </div>
      </div>
    );
  }

  return (
    <div className="entry__grid">
      {data.map((collection) => (
        <Link
          key={collection.collection_id}
          to={`/c/${encodeURIComponent(collection.collection_id)}`}
          className="entry__card"
        >
          <span>
            <span className="entry__name" title={collection.collection_id}>
              {formatCollection(collection)}
            </span>
            <br />
            <span className="entry__meta">
              {plural(collection.document_count, "document")} ·{" "}
              {plural(collection.fact_count, "fact")}
            </span>
          </span>
          <span className="entry__arrow" aria-hidden="true">
            →
          </span>
        </Link>
      ))}
    </div>
  );
}

export default function Landing() {
  return (
    <div className="landing">
      <NavBar />

      <div className="landing__body">
        <h1 className="hero__title">Turn documents into grounded facts</h1>
        <p className="hero__lede">
          Upload a set of PDFs. Every claim is extracted, tied to its page, and
          reconciled against the rest.
        </p>

        {/*
          Ingestion is a write path, and this interface is read-only, so the
          drop zone is shown as the design has it but left inert rather than
          wired to POST /collections/{id}/documents.
        */}
        <div className="dropzone">
          <div className="dropzone__inner">
            <div className="dropzone__icon" aria-hidden="true">
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none">
                <path
                  d="M8 13V3M8 3L4 7M8 3l4 4"
                  stroke="currentColor"
                  strokeWidth="1.4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </div>
            <div className="dropzone__title">Drop PDFs here</div>
            <div className="dropzone__hint">or choose files · up to 50 documents</div>
            <button type="button" className="btn-accent" aria-disabled="true" disabled>
              Upload
            </button>
            <p className="dropzone__note">
              Ingestion runs offline through <code>scripts/ingest.py</code>; this
              interface only reads.
            </p>
          </div>
        </div>

        <div className="entry">
          <div className="entry__label">Collections</div>
          <Collections />
        </div>
      </div>

      <footer className="landing__footer">Extract · Ground · Reconcile</footer>
    </div>
  );
}
