import NavBar from "./NavBar.jsx";

export default function About() {
  return (
    <div>
      <NavBar />
      <div className="prose">
        <h1>About</h1>
        <p>
          This interface reads a knowledge layer built from PDFs. Each document
          is parsed, a model proposes facts with a verbatim quote, and the quote
          is searched for in the file itself — so the page and the bounding box
          behind every fact come from the document, never from the model.
        </p>

        <h2>Why the evidence matters</h2>
        <p>
          A fact is only worth as much as what stands behind it. Every row here
          opens the page it came from, rendered as an image with the supporting
          span boxed, so a claim can be checked rather than taken on trust. A
          quote that cannot be found on the page is dropped instead of being
          stored against a guessed page number.
        </p>

        <h2>Corroborate, contradict, reconcile</h2>
        <p>
          Facts that describe the same subject and attribute are compared once
          their units and periods are normalized. Two documents agreeing is a
          corroboration; disagreeing on the same basis is a contradiction. Most
          apparent conflicts are neither — they are the same quantity measured
          over a different period or on a different basis, and those are marked
          reconcilable with the difference stated.
        </p>

        <h2>Read-only</h2>
        <p>
          Ingestion and reconciliation are write paths that run offline through{" "}
          <code>scripts/ingest.py</code>. This interface only issues GET
          requests.
        </p>
      </div>
    </div>
  );
}
