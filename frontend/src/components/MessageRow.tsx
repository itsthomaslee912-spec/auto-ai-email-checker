import { memo, type CSSProperties } from "react";
import type { EmailItem, EmailLabel, MailFolder } from "../api";
import { CLASSIFY_LABEL_TITLES, INTERVIEW_SUBTYPE_TITLES } from "../labels";
import type { ViewMode } from "../prefs";

function MessageRow({
  email,
  viewMode,
  active,
  accountAddress,
  accountTint,
  when,
  folderTitle,
  folder,
  onOpen,
  onDoubleOpen,
  onPrefetch,
}: {
  email: EmailItem;
  viewMode: ViewMode;
  active: boolean;
  accountAddress: string;
  accountTint?: string;
  when: { time: string; day: string };
  folderTitle: string;
  folder: MailFolder;
  onOpen: (id: number) => void;
  onDoubleOpen: (id: number) => void;
  onPrefetch: (id: number) => void;
}) {
  const itemClass = [
    "msg-item",
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
        onClick={() => onOpen(email.id)}
        onDoubleClick={() => onDoubleOpen(email.id)}
        onPointerEnter={() => onPrefetch(email.id)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            onDoubleOpen(email.id);
          }
        }}
      >
        <span className="table-cell">
          <strong className="table-primary">
            {senderName(email.sender)}
            {(email.thread_count ?? 1) > 1 && <span className="thread-count">{email.thread_count}</span>}
          </strong>
          {accountAddress && <AccountChip address={accountAddress} tint={accountTint} />}
        </span>
        <span className="table-cell">
          <span className="table-primary table-subject">{email.subject || "(no subject)"}</span>
          <span className="table-secondary">{email.snippet}</span>
        </span>
        <span className="table-cell">
          <span className={`label list-label email-category-label label-${email.label}`}>
            {categoryTitle}{subtypeTitle ? ` / ${subtypeTitle}` : ""}
          </span>
        </span>
        <span className="table-cell table-folder"><span className={`folder-pill folder-${folder}`} title={`Folder: ${folderTitle}`}>{folderTitle}</span></span>
        <span className="table-cell table-status"><span className={email.is_read ? "read-pill" : "unread-pill"}>{email.is_read ? "Read" : "Unread"}</span></span>
        <time className="table-cell table-date">
          <span className="table-primary">{when.day || when.time}</span>
          {when.day && <span className="table-secondary">{when.time}</span>}
        </time>
        {!email.is_read && <span className="unread-corner-badge" aria-label="Unread">NEW</span>}
      </button>
    );
  }

  return (
    <button type="button" className={itemClass} onClick={() => onOpen(email.id)} onPointerEnter={() => onPrefetch(email.id)}>
      <span className={`avatar soft label-${email.label}`}>
        {initialsFrom(senderName(email.sender))}
      </span>
      <span className="msg-body">
        <span className="msg-top">
          <span className="msg-from">
            <span className="msg-sender-line">
              <strong>{senderName(email.sender)}</strong>
              {(email.thread_count ?? 1) > 1 && <span className="thread-count">{email.thread_count}</span>}
            </span>
            {accountAddress ? <AccountChip address={accountAddress} tint={accountTint} /> : null}
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
          <span className={`folder-pill folder-${folder}`} title={`Folder: ${folderTitle}`}>{folderTitle}</span>
        </span>
        <span className="msg-snippet">{email.snippet}</span>
      </span>
      {!email.is_read && <span className="unread-corner-badge" aria-label="Unread">NEW</span>}
    </button>
  );
}

function sameMessageRow(prev: Parameters<typeof MessageRow>[0], next: Parameters<typeof MessageRow>[0]): boolean {
  return prev.email === next.email
    && prev.viewMode === next.viewMode
    && prev.active === next.active
    && prev.accountAddress === next.accountAddress
    && prev.accountTint === next.accountTint
    && prev.folderTitle === next.folderTitle
    && prev.folder === next.folder
    && prev.onOpen === next.onOpen
    && prev.onDoubleOpen === next.onDoubleOpen
    && prev.onPrefetch === next.onPrefetch;
}

export default memo(MessageRow, sameMessageRow);

function AccountChip({ address, tint }: { address: string; tint?: string }) {
  const at = address.indexOf("@");
  const name = at > 0 ? address.slice(0, at) : address;
  const domain = at > 0 ? address.slice(at) : "";
  return (
    <span className="msg-account" title={address} style={tint ? ({ "--account-color": tint } as CSSProperties) : undefined}>
      <span className="msg-account-dot" aria-hidden="true" />
      <span className="msg-account-text">{name}<span className="msg-account-domain">{domain}</span></span>
    </span>
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
