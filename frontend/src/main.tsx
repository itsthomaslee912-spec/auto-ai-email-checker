import { createRoot } from "react-dom/client";
import AppTooltip from "./components/AppTooltip";
import AuthGate from "./components/AuthGate";
import "./index.css";
import { applyAppearancePrefs } from "./prefs";
import { applyTheme, loadThemePref } from "./theme";

applyTheme(loadThemePref());
applyAppearancePrefs();

// No StrictMode: the app is served by the Vite dev server, where StrictMode
// mounts everything twice and doubles every startup request.
createRoot(document.getElementById("root")!).render(
  <>
    <AuthGate />
    <AppTooltip />
  </>
);
