import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { RootErrorBoundary } from "./RootErrorBoundary";
import "./style.css";
import "../../shared/typography.css";
import "../../shared/backdrop.css";
import "./rebrand.css";
import { fitTiles } from "./fitTiles";

fitTiles();
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RootErrorBoundary><App /></RootErrorBoundary>
  </React.StrictMode>,
);
