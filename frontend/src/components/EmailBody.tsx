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

function buildHtmlDocument(html: string, theme: AppTheme): string {
  const dark = theme === "dark";
  const page = dark ? "#171b24" : "#ffffff";
  const ink = dark ? "#eef2f7" : "#1b2433";
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
    () => (htmlToRender ? buildHtmlDocument(htmlToRender, theme) : ""),
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
          srcDoc={framedHtml}
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
