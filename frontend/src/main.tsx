import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import AppTooltip from "./components/AppTooltip";
import "./index.css";
import { applyAppearancePrefs } from "./prefs";
import { applyTheme, loadThemePref } from "./theme";

applyTheme(loadThemePref());
applyAppearancePrefs();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
    <AppTooltip />
  </StrictMode>
);
