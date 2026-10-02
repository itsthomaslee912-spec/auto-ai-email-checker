export type ViewMode = "list" | "table";
export type InboxType = "default" | "unread_first";
export type ReadingPane = "no_split" | "right" | "below";
export type FontFamily =
  | "dm-sans"
  | "inter"
  | "manrope"
  | "poppins"
  | "nunito"
  | "roboto"
  | "source-serif"
  | "merriweather"
  | "ibm-plex"
  | "jetbrains-mono"
  | "system";
export type FontSize = "extra-small" | "small" | "medium" | "large" | "extra-large" | "huge";

const VIEW_KEY = "email-checker-view";
const INBOX_TYPE_KEY = "email-checker-inbox-type";
const READING_PANE_KEY = "email-checker-reading-pane";
const FONT_KEY = "email-checker-font";
const FONT_SIZE_KEY = "email-checker-font-size";

export const VIEW_MODES: ViewMode[] = ["list", "table"];
export const INBOX_TYPES: InboxType[] = ["default", "unread_first"];
export const READING_PANES: { id: ReadingPane; label: string }[] = [
  { id: "no_split", label: "No Split" },
  { id: "right", label: "Right of inbox" },
  { id: "below", label: "Below inbox" },
];
export const FONT_FAMILIES: { id: FontFamily; label: string }[] = [
  { id: "dm-sans", label: "DM Sans" },
  { id: "inter", label: "Inter" },
  { id: "manrope", label: "Manrope" },
  { id: "poppins", label: "Poppins" },
  { id: "nunito", label: "Nunito" },
  { id: "roboto", label: "Roboto" },
  { id: "source-serif", label: "Source Serif" },
  { id: "merriweather", label: "Merriweather" },
  { id: "ibm-plex", label: "IBM Plex Sans" },
  { id: "jetbrains-mono", label: "JetBrains Mono" },
  { id: "system", label: "System UI" },
];
export const FONT_SIZES: { id: FontSize; label: string }[] = [
  { id: "extra-small", label: "Extra small" },
  { id: "small", label: "Small" },
  { id: "medium", label: "Medium" },
  { id: "large", label: "Large" },
  { id: "extra-large", label: "Extra large" },
  { id: "huge", label: "Huge" },
];

function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

export function loadViewMode(): ViewMode {
  const value = readStorage(VIEW_KEY);
  if (value === "list" || value === "table") return value;
  return "list";
}

export function saveViewMode(mode: ViewMode): void {
  writeStorage(VIEW_KEY, mode);
}

export function loadInboxType(): InboxType {
  const value = readStorage(INBOX_TYPE_KEY);
  if (value === "default" || value === "unread_first") return value;
  return "default";
}

export function saveInboxType(type: InboxType): void {
  writeStorage(INBOX_TYPE_KEY, type);
}

export function loadReadingPane(): ReadingPane {
  const value = readStorage(READING_PANE_KEY);
  if (value === "no_split" || value === "right" || value === "below") return value;
  return "right";
}

export function saveReadingPane(pane: ReadingPane): void {
  writeStorage(READING_PANE_KEY, pane);
}


export function loadFontFamily(): FontFamily {
  const value = readStorage(FONT_KEY);
  if (
    value === "dm-sans" ||
    value === "inter" ||
    value === "manrope" ||
    value === "poppins" ||
    value === "nunito" ||
    value === "roboto" ||
    value === "source-serif" ||
    value === "merriweather" ||
    value === "ibm-plex" ||
    value === "jetbrains-mono" ||
    value === "system"
  ) {
    return value;
  }
  return "dm-sans";
}

export function applyFontFamily(font: FontFamily): void {
  document.documentElement.dataset.font = font;
}

export function saveFontFamily(font: FontFamily): void {
  writeStorage(FONT_KEY, font);
  applyFontFamily(font);
}

export function loadFontSize(): FontSize {
  const value = readStorage(FONT_SIZE_KEY);
  if (
    value === "extra-small" ||
    value === "small" ||
    value === "medium" ||
    value === "large" ||
    value === "extra-large" ||
    value === "huge"
  ) return value;
  return "medium";
}

export function applyFontSize(size: FontSize): void {
  document.documentElement.dataset.fontSize = size;
}

export function saveFontSize(size: FontSize): void {
  writeStorage(FONT_SIZE_KEY, size);
  applyFontSize(size);
}

export function applyAppearancePrefs(): void {
  applyFontFamily(loadFontFamily());
  applyFontSize(loadFontSize());
}
