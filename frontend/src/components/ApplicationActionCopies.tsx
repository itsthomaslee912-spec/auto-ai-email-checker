import { useMemo, useState } from "react";
import { extractApplicationCopyActions } from "../utils/applicationActions";
import { copyText } from "../utils/clipboard";

type Props = {
  subject?: string | null;
  html?: string | null;
  text?: string | null;
  snippet?: string | null;
};

function CopyIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M6 15H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v1" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="m5 12 5 5L20 7" />
    </svg>
  );
}

function LinkIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M10 13a5 5 0 0 0 7.07 0l1.41-1.41a5 5 0 0 0-7.07-7.07L10 6" />
      <path d="M14 11a5 5 0 0 0-7.07 0L5.5 12.4a5 5 0 0 0 7.07 7.07L14 18" />
    </svg>
  );
}

export default function ApplicationActionCopies({ subject, html, text, snippet }: Props) {
  const actions = useMemo(
    () => extractApplicationCopyActions({ subject, html, text, snippet }),
    [subject, html, text, snippet],
  );
  const [copiedId, setCopiedId] = useState<string | null>(null);

  if (!actions.length) return null;

  async function onCopy(id: string, value: string) {
    await copyText(value);
    setCopiedId(id);
    window.setTimeout(() => setCopiedId((current) => (current === id ? null : current)), 1800);
  }

  return (
    <div className="application-action-bar" aria-label="Application actions">
      <p className="application-action-hint">Quick copy</p>
      <div className="application-action-copies">
        {actions.map((action) => {
          const copied = copiedId === action.id;
          const tip = copied ? "Copied" : `Copy ${action.label.toLowerCase()}`;
          return (
            <button
              key={action.id}
              type="button"
              className={`application-action-copy${copied ? " is-copied" : ""}`}
              onClick={() => void onCopy(action.id, action.value)}
              aria-label={`${tip}: ${action.value}`}
              data-tooltip={tip}
              data-tooltip-placement="bottom"
              title={`${action.label}: ${action.value}`}
            >
              <span className="application-action-copy-icon" aria-hidden="true">
                {copied ? <CheckIcon /> : action.kind === "link" ? <LinkIcon /> : <CopyIcon />}
              </span>
              <span className="application-action-copy-meta">
                <span className="application-action-copy-kind">
                  {copied ? "Copied" : action.kind === "code" ? "Code" : "Link"}
                </span>
                <span className="application-action-copy-value">
                  {action.kind === "code" ? action.value : "Open link"}
                </span>
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
