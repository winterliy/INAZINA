// Fonts are bundled (no request to Google: the privacy policy says no visitor data leaves).
import "@fontsource/archivo/500.css";
import "@fontsource/archivo/700.css";
import "@fontsource/archivo/800.css";
import "@fontsource/dm-sans/400.css";
import "@fontsource/dm-sans/500.css";
import "@fontsource/dm-sans/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "@fontsource/ibm-plex-sans-kr/400.css";
import "@fontsource/ibm-plex-sans-kr/500.css";
import "@fontsource/ibm-plex-sans-kr/600.css";
import "@fontsource/ibm-plex-sans-kr/700.css";
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { DialogHost } from "../../shared/dialogs";
import { RootErrorBoundary } from "./RootErrorBoundary";
import "./style.css";
import "../../shared/typography.css";
import "../../shared/backdrop.css";
import "./rebrand.css";
import { fitTiles } from "./fitTiles";

fitTiles();
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RootErrorBoundary><App /><DialogHost /></RootErrorBoundary>
  </React.StrictMode>,
);
