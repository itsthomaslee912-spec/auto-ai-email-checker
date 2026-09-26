export type ThemePref = "dark" | "light" | "system";

const THEME_KEY = "email-checker-theme";

export function loadThemePref(): ThemePref {
  try {
    const value = localStorage.getItem(THEME_KEY);
    if (value === "dark" || value === "light" || value === "system") return value;
  } catch {
    /* ignore */
  }
  return "system";
}

export function resolvedTheme(pref: ThemePref): "dark" | "light" {
  if (pref === "light" || pref === "dark") return pref;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function applyTheme(pref: ThemePref): void {
  document.documentElement.dataset.theme = resolvedTheme(pref);
}

export function saveThemePref(pref: ThemePref): void {
  try {
    localStorage.setItem(THEME_KEY, pref);
  } catch {
    /* ignore */
  }
  applyTheme(pref);
}
