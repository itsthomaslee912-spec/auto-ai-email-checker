import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import AppTooltip from "./components/AppTooltip";
import AuthGate from "./components/AuthGate";
import "./index.css";
import { applyAppearancePrefs } from "./prefs";
import { applyTheme, loadThemePref } from "./theme";

applyTheme(loadThemePref());
applyAppearancePrefs();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthGate />
    <AppTooltip />
  </StrictMode>
);
