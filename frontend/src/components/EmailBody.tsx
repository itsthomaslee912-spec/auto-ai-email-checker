import { useEffect, useMemo, useState } from "react";

type AppTheme = "dark" | "light";

function useAppTheme(): AppTheme {
  const [theme, setTheme] = useState<AppTheme>(() =>
    document.documentElement.dataset.theme === "light" ? "light" : "dark",
  );
  useEffect(() => {
    const root = document.documentElement;
    const sync = () => setTheme(root.dataset.theme === "light" ? "light" : "dark");
    const observer = new MutationObserver(sync);
    observer.observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);
  return theme;
}

function looksLikeHtml(value: string): boolean {
  return /<\/?[a-z][\s\S]*>/i.test(value);
}

function splitMixedPlainHtml(value: string): { html: string; text: string } {
  const match = value.match(/<[a-z][\s\S]*>/i);
  if (!match || match.index == null) return { html: "", text: value.trim() };
  if (match.index === 0) return { html: value.trim(), text: "" };
  return {
    text: value.slice(0, match.index).trim(),
    html: value.slice(match.index).trim(),
  };
}

const PALETTE = {
  dark: { page: "#171b24", ink: "#eef2f7" },
  light: { page: "#ffffff", ink: "#1b2433" },
} as const;
const MIN_CONTRAST = 4.5;

type Rgb = [number, number, number];

let colorProbe: HTMLSpanElement | null = null;
const colorCache = new Map<string, Rgb | null>();

/** Resolve any CSS color (names, hex, rgb, hsl) through the browser; null for transparent or invalid. */
function parseColor(value: string | null | undefined): Rgb | null {
  const key = (value || "").trim().toLowerCase();
  if (!key || key === "inherit" || key === "initial" || key === "currentcolor") return null;
  if (colorCache.has(key)) return colorCache.get(key)!;
  if (!colorProbe) {
    colorProbe = document.createElement("span");
    colorProbe.style.display = "none";
    document.body.appendChild(colorProbe);
  }
  colorProbe.style.color = "";
  colorProbe.style.color = key;
  let result: Rgb | null = null;
  if (colorProbe.style.color) {
    const match = getComputedStyle(colorProbe).color.match(/[\d.]+/g);
    if (match && (match.length < 4 || Number(match[3]) > 0.5)) {
      result = [Number(match[0]), Number(match[1]), Number(match[2])];
    }
  }
  colorCache.set(key, result);
  return result;
}

function luminance([r, g, b]: Rgb): number {
  const channel = (value: number) => {
    const c = value / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function rgbToHsl([r, g, b]: Rgb): Rgb {
  const [rn, gn, bn] = [r / 255, g / 255, b / 255];
  const max = Math.max(rn, gn, bn);
  const min = Math.min(rn, gn, bn);
  const l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min;
  const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
  const h = max === rn ? (gn - bn) / d + (gn < bn ? 6 : 0) : max === gn ? (bn - rn) / d + 2 : (rn - gn) / d + 4;
  return [h / 6, s, l];
}

function hslToRgb([h, s, l]: Rgb): Rgb {
  if (s === 0) return [l * 255, l * 255, l * 255];
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  const hue = (t: number) => {
    const x = t < 0 ? t + 1 : t > 1 ? t - 1 : t;
    if (x < 1 / 6) return p + (q - p) * 6 * x;
    if (x < 1 / 2) return q;
    if (x < 2 / 3) return p + (q - p) * (2 / 3 - x) * 6;
    return p;
  };
  return [hue(h + 1 / 3) * 255, hue(h) * 255, hue(h - 1 / 3) * 255];
}

/** Move a color's lightness away from the background until it is readable, keeping its hue. */
function readableOn(color: Rgb, background: Rgb, fallback: Rgb): Rgb {
  const [h, s, l] = rgbToHsl(color);
  if (s < 0.15) return fallback;
  const lighten = luminance(background) < 0.4;
  for (let step = 1; step <= 20; step += 1) {
    const next = Math.min(1, Math.max(0, l + (lighten ? 0.05 : -0.05) * step));
    const candidate = hslToRgb([h, Math.min(s, 0.85), next]);
    if (contrast(candidate, background) >= MIN_CONTRAST) return candidate;
  }
  return fallback;
}

const toCss = ([r, g, b]: Rgb) => `rgb(${Math.round(r)}, ${Math.round(g)}, ${Math.round(b)})`;

/**
 * Email authors hard-code colors for a white page. Re-tint only the inline
 * colors that become unreadable on the current theme, including theme ink
 * inherited into boxes that carry their own light or dark background.
 */
function fitColorsToTheme(html: string, theme: AppTheme): string {
  const { page, ink } = PALETTE[theme];
  const pageRgb = parseColor(page)!;
  const inkRgb = parseColor(ink)!;
  const doc = new DOMParser().parseFromString(html, "text/html");

  const walk = (element: Element, color: Rgb, background: Rgb) => {
    const style = (element as HTMLElement).style;
    const ownBackground = parseColor(style?.backgroundColor) ?? parseColor(element.getAttribute("bgcolor"));
    const ownColor = parseColor(style?.color) ?? (element.tagName === "FONT" ? parseColor(element.getAttribute("color")) : null);
    let nextColor = ownColor ?? color;
    const nextBackground = ownBackground ?? background;
    if ((ownColor || ownBackground) && style && contrast(nextColor, nextBackground) < MIN_CONTRAST) {
      const surfaceInk = PALETTE[luminance(nextBackground) < 0.4 ? "dark" : "light"].ink;
      nextColor = readableOn(nextColor, nextBackground, parseColor(surfaceInk)!);
      style.setProperty("color", toCss(nextColor), "important");
      if (element.tagName === "FONT") element.removeAttribute("color");
    }
    for (const child of Array.from(element.children)) walk(child, nextColor, nextBackground);
  };
  for (const child of Array.from(doc.body.children)) walk(child, inkRgb, pageRgb);

  const headStyles = Array.from(doc.head.querySelectorAll("style")).map((node) => node.outerHTML).join("");
  return headStyles + doc.body.innerHTML;
}

function buildHtmlDocument(html: string, theme: AppTheme): string {
  const dark = theme === "dark";
  const { page, ink } = PALETTE[theme];
  const heading = dark ? "#eef2f7" : "#1b2433";
  const link = dark ? "#4c8dff" : "#2f6fed";
  const quote = dark ? "#8b95a7" : "#5c6778";
  const quoteLine = dark ? "rgba(255,255,255,.18)" : "rgba(20,32,48,.18)";
  const track = dark ? "#12151b" : "#e9edf4";
  const thumb = dark ? "#4a5568" : "#b0b8c6";
  const thumbHover = dark ? "#6b778c" : "#8b95a7";
  return `<!DOCTYPE html>
<html style="color-scheme: ${theme}">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  * { box-sizing: border-box; }
  html {
    height: 100%;
    color-scheme: ${theme};
    scrollbar-width: thin;
    scrollbar-color: ${thumb} ${track};
  }
  html::-webkit-scrollbar { width: 12px; height: 12px; }
  html::-webkit-scrollbar-track {
    background: ${track};
    border-radius: 8px;
  }
  html::-webkit-scrollbar-thumb {
    background: ${thumb};
    border-radius: 8px;
    border: 3px solid ${track};
  }
  html::-webkit-scrollbar-thumb:hover { background: ${thumbHover}; }

  body {
    margin: 0;
    min-height: 100%;
    padding: 20px 22px 28px;
    background: ${page};
    color: ${ink};
    font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 16px;
    line-height: 1.7;
    letter-spacing: 0.01em;
    word-break: break-word;
    overflow-wrap: anywhere;
  }
  .email-canvas {
    max-width: 680px;
    margin: 0 auto;
  }
  p { margin: 0 0 1em; }
  h1, h2, h3, h4 { line-height: 1.3; color: ${heading}; }
  img, video {
    max-width: 100% !important;
    height: auto !important;
  }
  a { color: ${link}; }
  table {
    max-width: 100% !important;
    border-collapse: collapse;
  }
  td, th { word-break: break-word; }
  pre, code {
    white-space: pre-wrap;
    word-break: break-word;
    font-size: 0.95em;
  }
  blockquote {
    margin: 0.75em 0;
    padding-left: 0.9em;
    border-left: 3px solid ${quoteLine};
    color: ${quote};
  }
</style>
</head>
<body><div class="email-canvas">${html}</div></body>
</html>`;
}

type Props = {
  html?: string | null;
  text?: string | null;
  snippet?: string | null;
};

export default function EmailBody({ html, text, snippet }: Props) {
  const theme = useAppTheme();
  const sourceHtml = (html || "").trim();
  const sourceText = (text || "").trim();
  const fallback = (snippet || "").trim();

  const htmlToRender = useMemo(() => {
    if (sourceHtml) {
      const mixed = splitMixedPlainHtml(sourceHtml);
      return mixed.html || sourceHtml;
    }
    if (!sourceText) return "";
    const mixed = splitMixedPlainHtml(sourceText);
    if (mixed.html) return mixed.html;
    if (looksLikeHtml(sourceText)) return sourceText;
    return "";
  }, [sourceHtml, sourceText]);

  const framedHtml = useMemo(
    () => (htmlToRender ? buildHtmlDocument(fitColorsToTheme(htmlToRender, theme), theme) : ""),
    [htmlToRender, theme],
  );

  const plainToRender = useMemo(() => {
    if (htmlToRender) return "";
    if (!sourceText) return fallback;
    const mixed = splitMixedPlainHtml(sourceText);
    return mixed.text || sourceText || fallback;
  }, [htmlToRender, sourceText, fallback]);

  if (htmlToRender) {
    return (
      <div className="email-body-scroll">
        <iframe
          className="email-body-frame"
          aria-label="Email body"
          sandbox=""
          tabIndex={-1}
          srcDoc={framedHtml}
          onFocus={(event) => event.currentTarget.blur()}
        />
      </div>
    );
  }

  return (
    <div className="email-body-scroll plain-scroll">
      <div className="reader-body plain">{plainToRender || "No message body."}</div>
    </div>
  );
}
