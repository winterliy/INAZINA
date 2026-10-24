import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { BoardPage } from "./BoardPage";
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
    <Root />
  </React.StrictMode>,
);
