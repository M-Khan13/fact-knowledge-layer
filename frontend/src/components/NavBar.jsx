import { Link } from "react-router-dom";

// Left as a placeholder for the repository to be filled in.
const SOURCE_URL = "https://github.com/M-Khan13/...";

/**
 * The workspace already carries the wordmark in its sidebar, so there it is
 * only the links that are wanted; the landing page shows both.
 */
export default function NavBar({ flush = false, wordmark = true }) {
  return (
    <nav className={flush ? "nav nav--flush" : "nav"}>
      {wordmark ? (
        <Link to="/" className="wordmark">
          <span className="wordmark__mark" aria-hidden="true" />
          Fact Knowledge Layer
        </Link>
      ) : (
        <span />
      )}
      <div className="nav__links">
        <a className="nav__link" href={SOURCE_URL} target="_blank" rel="noreferrer">
          Source code
        </a>
        <Link className="nav__link" to="/about">
          About
        </Link>
      </div>
    </nav>
  );
}
