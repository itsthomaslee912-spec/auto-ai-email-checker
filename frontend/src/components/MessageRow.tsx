import type { EmailItem, EmailLabel, MailFolder } from "../api";
import { CLASSIFY_LABEL_TITLES, INTERVIEW_SUBTYPE_TITLES } from "../labels";
import type { ViewMode } from "../prefs";

export default function MessageRow({
  email,
  viewMode,
  active,
  accountAddress,
  when,
  folderTitle,
  folder,
  onOpen,
  onDoubleOpen,
}: {
  email: EmailItem;
  viewMode: ViewMode;
  active: boolean;
  accountAddress: string;
  when: { time: string; day: string };
  folderTitle: string;
  folder: MailFolder;
  onOpen: (id: number) => void;
  onDoubleOpen: (id: number) => void;
}) {
  const itemClass = [
    viewMode === "card" ? "msg-card" : "msg-item",
    active ? "active" : "",
    email.is_read ? "" : "unread",
  ]
    .filter(Boolean)
    .join(" ");
  const categoryTitle = CLASSIFY_LABEL_TITLES[email.label as EmailLabel] ?? email.label;
  const subtypeTitle = email.label === "interview_scheduled" && email.interview_subtype
    ? INTERVIEW_SUBTYPE_TITLES[email.interview_subtype]
    : null;

  if (viewMode === "table") {
    return (
      <button
        type="button"
        className={`msg-table table-columns${active ? " active" : ""}${email.is_read ? "" : " unread"}`}
        title="Double-click to open email"
        onClick={() => onOpen(email.id)}
        onDoubleClick={() => onDoubleOpen(email.id)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            onDoubleOpen(email.id);
          }
        }}
      >
        <span className="table-cell">
          <strong className="table-primary" title={email.sender}>{senderName(email.sender)}</strong>
          {accountAddress && <span className="table-secondary" title={accountAddress}>{accountAddress}</span>}
        </span>
        <span className="table-cell">
          <span className="table-primary table-subject" title={email.subject || "(no subject)"}>{email.subject || "(no subject)"}</span>
          <span className="table-secondary" title={email.snippet}>{email.snippet}</span>
        </span>
        <span className="table-cell">
          <span className={`label list-label email-category-label label-${email.label}`}>
            {categoryTitle}{subtypeTitle ? ` / ${subtypeTitle}` : ""}
          </span>
        </span>
        <span className="table-cell table-folder"><span className={`folder-pill folder-${folder}`}>{folderTitle}</span></span>
        <span className="table-cell table-status"><span className={email.is_read ? "read-pill" : "new-pill"}>{email.is_read ? "Read" : "NEW"}</span></span>
        <time className="table-cell table-date">
          <span className="table-primary">{when.day || when.time}</span>
          {when.day && <span className="table-secondary">{when.time}</span>}
        </time>
      </button>
    );
  }

  return (
    <button type="button" className={itemClass} onClick={() => onOpen(email.id)}>
      <span className={`avatar soft label-${email.label}`}>
        {initialsFrom(senderName(email.sender))}
      </span>
      <span className="msg-body">
        <span className="msg-top">
          <span className="msg-from">
            <strong>{senderName(email.sender)}</strong>
            {accountAddress ? <span className="msg-account">{accountAddress}</span> : null}
          </span>
          <span className="msg-meta">
            <time>
              <span className="msg-time">{when.time}</span>
              {when.day && <span className="msg-day">{when.day}</span>}
            </time>
          </span>
        </span>
        <span className="msg-subject">{email.subject || "(no subject)"}</span>
        <span className="msg-details">
          <span className={`label list-label email-category-label label-${email.label}`}>
            {categoryTitle}{subtypeTitle ? ` / ${subtypeTitle}` : ""}
          </span>
          <span className={`folder-pill folder-${folder}`}>{folderTitle}</span>
          <span className={email.is_read ? "read-pill" : "new-pill"}>
            {email.is_read ? "Read" : "NEW"}
          </span>
        </span>
        <span className="msg-snippet">{email.snippet}</span>
      </span>
    </button>
  );
}

function senderName(sender: string): string {
  const match = sender.match(/^"?([^"<]+)"?\s*</);
  if (match?.[1]) return match[1].trim();
  return sender || "Unknown";
}

function initialsFrom(text: string): string {
  const clean = text.replace(/@.*/, "").replace(/[^a-zA-Z0-9 ]/g, " ").trim();
  const parts = clean.split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}
