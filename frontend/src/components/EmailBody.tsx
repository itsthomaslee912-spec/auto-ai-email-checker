import { useMemo } from "react";

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

function buildHtmlDocument(html: string): string {
  return `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  * { box-sizing: border-box; }
  html {
    height: 100%;
    scrollbar-width: thin;
    scrollbar-color: #9aa3b2 #eef1f6;
  }
  html::-webkit-scrollbar { width: 12px; height: 12px; }
  html::-webkit-scrollbar-track {
    background: #eef1f6;
    border-radius: 8px;
  }
  html::-webkit-scrollbar-thumb {
    background: #9aa3b2;
    border-radius: 8px;
    border: 3px solid #eef1f6;
  }
  html::-webkit-scrollbar-thumb:hover { background: #6f7a8d; }

  body {
    margin: 0;
    min-height: 100%;
    padding: 20px 22px 28px;
    background: #ffffff;
    color: #1f2430;
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
  h1, h2, h3, h4 { line-height: 1.3; color: #12151c; }
  img, video {
    max-width: 100% !important;
    height: auto !important;
  }
  a { color: #0b57d0; }
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
    border-left: 3px solid #d5dbe7;
    color: #4a5160;
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
          title="Email body"
          sandbox=""
          srcDoc={buildHtmlDocument(htmlToRender)}
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
