import { useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import NavBar from "./NavBar.jsx";
import FactsTable from "./FactsTable.jsx";
import RelationshipCard from "./RelationshipCard.jsx";
import EvidencePanel from "./EvidencePanel.jsx";
import useResource from "../useResource.js";
import { getCollection, listCollections, listFacts, listRelationships } from "../api.js";
import {
  VERDICTS,
  formatAttribute,
  formatCollection,
  formatPeriod,
  plural,
  verdictOf,
} from "../format.js";

const PAGE_SIZE = 12;

// How many facts show before the table is expanded. A collection runs to a
// couple of hundred, and the relationships underneath are the point of the
// screen, so the table opens as a sample rather than a wall.
const FACT_PREVIEW = 5;

function Sidebar({ activeId }) {
  const { data } = useResource((options) => listCollections(options), []);

  return (
    <aside className="sidebar">
      <Link to="/" className="wordmark">
        <span className="wordmark__mark" aria-hidden="true" />
        Fact Knowledge Layer
      </Link>

      <div>
        <div className="sidebar__section-label">Collections</div>
        <nav className="sidebar__list">
          {(data || []).map((collection) => {
            const active = collection.collection_id === activeId;
            return (
              <Link
                key={collection.collection_id}
                to={`/c/${encodeURIComponent(collection.collection_id)}`}
                className={`sidebar__item${active ? " sidebar__item--active" : ""}`}
                title={collection.collection_id}
              >
                <span className="sidebar__swatch" aria-hidden="true" />
                {formatCollection(collection)}
              </Link>
            );
          })}
        </nav>
      </div>
    </aside>
  );
}

function Facts({ facts, onViewSource }) {
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(false);
  const head = useRef(null);

  // 200-odd facts per collection is small enough to filter in the browser,
  // which keeps typing instant and the API untouched.
  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return facts;
    return facts.filter((fact) =>
      [fact.subject, formatAttribute(fact.attribute), formatPeriod(fact), fact.source_doc]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [facts, query]);

  const visible = expanded ? shown : shown.slice(0, FACT_PREVIEW);
  const remaining = shown.length - visible.length;

  const toggle = () => {
    // Collapsing from deep inside an expanded table would otherwise leave the
    // reader stranded below the section that just shrank.
    if (expanded) head.current?.scrollIntoView({ block: "start" });
    setExpanded((current) => !current);
  };

  return (
    <section ref={head}>
      <div className="section__head">
        <h2 className="section__title">Facts</h2>
        <span className="section__count">
          {query.trim() ? `${shown.length} of ${facts.length}` : plural(facts.length, "fact")}
        </span>
        <div className="section__spacer" />
        <input
          className="filter-input"
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Filter by subject, attribute, or period"
          aria-label="Filter facts"
        />
      </div>
      <FactsTable facts={visible} onViewSource={onViewSource} />

      {shown.length > FACT_PREVIEW ? (
        <div className="more-row">
          <button type="button" className="btn-ghost" onClick={toggle}>
            {expanded ? "Show fewer" : `Show more · ${remaining} remaining`}
          </button>
        </div>
      ) : null}
    </section>
  );
}

function Relationships({ payload, documentCount, onViewSource }) {
  const [verdict, setVerdict] = useState("all");
  const [limit, setLimit] = useState(PAGE_SIZE);

  const all = payload?.relationships || [];
  const shown = verdict === "all" ? all : all.filter((row) => row.verdict === verdict);
  const visible = shown.slice(0, limit);

  const choose = (next) => {
    setVerdict(next);
    setLimit(PAGE_SIZE);
  };

  // Only offer a verdict that this collection actually produced.
  const counts = payload?.verdicts || {};
  const available = Object.keys(VERDICTS).filter((key) => counts[key]);

  return (
    <section>
      <div className="section__head">
        <h2 className="section__title">Relationships</h2>
        <span className="section__count">
          {plural(all.length, "pair")} across {plural(documentCount, "document")}
        </span>
        <div className="section__spacer" />
        <div className="chips">
          <button
            type="button"
            className={`chip${verdict === "all" ? " chip--on" : ""}`}
            onClick={() => choose("all")}
          >
            All {all.length}
          </button>
          {available.map((key) => (
            <button
              key={key}
              type="button"
              className={`chip${verdict === key ? " chip--on" : ""}`}
              onClick={() => choose(key)}
            >
              {verdictOf(key).label} {counts[key]}
            </button>
          ))}
        </div>
      </div>

      {visible.length === 0 ? (
        <div className="panel">
          <div className="state">No relationships of that kind.</div>
        </div>
      ) : (
        <div className="rel-list">
          {visible.map((relationship) => (
            <RelationshipCard
              key={relationship.relationship_id}
              relationship={relationship}
              onViewSource={onViewSource}
            />
          ))}
          {shown.length > visible.length ? (
            <div className="more-row">
              <button
                type="button"
                className="btn-ghost"
                onClick={() => setLimit((current) => current + PAGE_SIZE)}
              >
                Show more · {shown.length - visible.length} remaining
              </button>
            </div>
          ) : null}
        </div>
      )}
    </section>
  );
}

export default function Workspace() {
  const { collectionId } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();

  const collection = useResource(
    (options) => getCollection(collectionId, options),
    [collectionId],
  );
  const facts = useResource((options) => listFacts(collectionId, options), [collectionId]);
  const relationships = useResource(
    (options) => listRelationships(collectionId, options),
    [collectionId],
  );

  const factList = facts.data?.facts || [];

  // The open fact lives in the URL, so an evidence view can be linked to and
  // the browser's back button closes the panel instead of leaving the page.
  const openFactId = searchParams.get("fact");
  const openFact = useMemo(() => {
    if (!openFactId) return null;
    const fromFacts = factList.find((fact) => fact.fact_id === openFactId);
    if (fromFacts) return fromFacts;
    // A fact reached from a relationship card is inlined there, and is not
    // necessarily in the facts list yet if that request is still in flight.
    for (const row of relationships.data?.relationships || []) {
      for (const side of ["fact_a", "fact_b"]) {
        const candidate = row.facts?.[side];
        if (candidate?.fact_id === openFactId) return candidate;
      }
    }
    return null;
  }, [openFactId, factList, relationships.data]);

  const viewSource = (factId) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      next.set("fact", factId);
      return next;
    });
  };

  const closeEvidence = () => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      next.delete("fact");
      return next;
    });
  };

  const error = collection.error || facts.error || relationships.error;

  return (
    <div className="workspace">
      <Sidebar activeId={collectionId} />

      <main className="main">
        <div className="main__header">
          <h1 className="page-title">
            {collection.data ? formatCollection(collection.data) : collectionId}
            {collection.data ? (
              <span className="page-title__meta">
                {plural(collection.data.document_count, "document")} ·{" "}
                {plural(collection.data.fact_count, "fact")}
              </span>
            ) : null}
          </h1>
          <NavBar flush wordmark={false} />
        </div>

        <div className="main__content">
          {error ? (
            <div className="panel">
              <div className="state state--error">
                {error.message}
                <div className="state__hint">
                  {error.status === 404
                    ? "That collection does not exist."
                    : "Check that the API is running, then reload."}
                </div>
              </div>
            </div>
          ) : (
            <>
              {facts.loading ? (
                <div className="panel">
                  <div className="state">Loading facts…</div>
                </div>
              ) : (
                <Facts facts={factList} onViewSource={viewSource} />
              )}

              {relationships.loading ? (
                <div className="panel">
                  <div className="state">Loading relationships…</div>
                </div>
              ) : (
                <Relationships
                  payload={relationships.data}
                  documentCount={collection.data?.document_count ?? 0}
                  onViewSource={viewSource}
                />
              )}
            </>
          )}
        </div>
      </main>

      {openFact ? <EvidencePanel fact={openFact} onClose={closeEvidence} /> : null}
    </div>
  );
}
