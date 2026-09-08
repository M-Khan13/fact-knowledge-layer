/**
 * The read side of the fact knowledge layer.
 *
 * Every function here is a GET. Ingestion and reconciliation are write paths
 * that run offline through `scripts/ingest.py`, so nothing in the UI posts:
 * the one request helper below hardcodes the method rather than taking it as
 * an argument, which makes a stray write a change to this file instead of an
 * accident at a call site.
 */

export const API_BASE = (
  import.meta.env.VITE_API_BASE_URL || "http://localhost:8000"
).replace(/\/$/, "");

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function get(path, { signal } = {}) {
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, { method: "GET", signal });
  } catch (cause) {
    if (cause.name === "AbortError") throw cause;
    // A dead backend fails here rather than at .json(), and the message the
    // browser gives ("Failed to fetch") does not say which server is missing.
    throw new ApiError(`Could not reach the API at ${API_BASE}`, 0);
  }

  if (!response.ok) {
    // FastAPI puts the useful part in `detail`; fall back to the status line.
    const detail = await response
      .json()
      .then((body) => body?.detail)
      .catch(() => null);
    throw new ApiError(detail || `Request failed (${response.status})`, response.status);
  }
  return response.json();
}

export const listCollections = (options) => get("/collections", options);

export const getCollection = (collectionId, options) =>
  get(`/collections/${encodeURIComponent(collectionId)}`, options);

export const listFacts = (collectionId, options) =>
  get(`/collections/${encodeURIComponent(collectionId)}/facts`, options);

export const listRelationships = (collectionId, options) =>
  get(
    `/collections/${encodeURIComponent(collectionId)}/relationships?include_facts=true`,
    options,
  );

/**
 * The evidence PNG is loaded by the browser as an <img>, not by fetch, so this
 * only has to turn the relative `evidence_url` a fact carries into an absolute
 * one. Rendering the page costs real time on the server, hence the caller
 * showing a loading state until the image fires onLoad.
 */
export const evidenceSrc = (fact) => `${API_BASE}${fact.evidence_url}`;
