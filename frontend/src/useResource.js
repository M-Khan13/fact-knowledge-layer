import { useCallback, useEffect, useState } from "react";

/**
 * Load something from the API, with the three states every screen needs.
 *
 * The request is aborted when the dependencies change or the component goes
 * away, so switching collections quickly cannot let a slow earlier response
 * land on top of a newer one.
 */
export default function useResource(loader, deps) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  // The loader closes over the deps the caller passes, so it is keyed on those
  // rather than on its own identity — an inline arrow would otherwise refetch
  // on every render.
  const run = useCallback(loader, deps);

  useEffect(() => {
    const controller = new AbortController();
    let live = true;

    setState((previous) => ({ ...previous, loading: true, error: null }));

    run({ signal: controller.signal })
      .then((data) => live && setState({ data, error: null, loading: false }))
      .catch((error) => {
        if (error.name === "AbortError" || !live) return;
        setState({ data: null, error, loading: false });
      });

    return () => {
      live = false;
      controller.abort();
    };
  }, [run]);

  return state;
}
