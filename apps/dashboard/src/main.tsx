import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./style.css";
import "../../shared/typography.css";
import "../../shared/backdrop.css";
import "./ux-fixes.css";
import "./topbar.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
