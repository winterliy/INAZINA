// Fonts are bundled (no request to Google: the privacy policy says no visitor data leaves).
import "@fontsource/rethink-sans/400.css";
import "@fontsource/rethink-sans/500.css";
import "@fontsource/rethink-sans/600.css";
import "@fontsource/rethink-sans/700.css";
import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { BoardPage } from "./BoardPage";
import { RootErrorBoundary } from "./RootErrorBoundary";
import { DialogHost } from "../../shared/dialogs";
import { NAVIGATE, interceptLinks } from "./nav";
import "./style.css";
import "../../shared/typography.css";
import "../../shared/backdrop.css";

/** Re-renders on in-app navigation; App stays mounted so its session and data carry over. */
function Root() {
  const [, setPath] = useState(location.pathname + location.search);
  useEffect(() => {
    const update = () => setPath(location.pathname + location.search);
    window.addEventListener(NAVIGATE, update);
    window.addEventListener("popstate", update);
    document.addEventListener("click", interceptLinks);
    return () => { window.removeEventListener(NAVIGATE, update); window.removeEventListener("popstate", update); document.removeEventListener("click", interceptLinks); };
  }, []);
  return /^\/board\/?$/.test(location.pathname) ? <BoardPage /> : <App />;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RootErrorBoundary><Root /><DialogHost /></RootErrorBoundary>
  </React.StrictMode>,
);
