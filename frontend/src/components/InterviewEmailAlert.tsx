import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import { accountColor } from "../accountColor";
import {
  openEvents,
  fetchInterviewEmailAlerts,
  fetchPendingInterviews,
  setPendingInterviewDismissed,
  type InterviewEmailAlert as MailAlert,
  type Mailbox,
  type PendingInterview,
} from "../api";

const SNOOZE_KEY = "interview-attention.snooze";
const SNOOZE_MS = 30 * 60_000;
const REFRESH_MS = 60_000;

type Snooze = { until: number; ids: number[] };
type RowKind = "invitation" | "reschedule" | "scheduled";
type Row = {
  emailId: number;
  mailboxId: number;
  account: string;
  kind: RowKind;
  subtype: string | null;
  company: string;
  role: string;
  subject: string;
  receivedAt: string | null;
  unread: boolean;
  startAt: string | null;
  slots: { start_at: string; end_at: string }[];
  pending: boolean;
};

const UNDO_MS = 6000;

function readSnooze(): Snooze | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(SNOOZE_KEY) || "null") as Snooze | null;
    return value && typeof value.until === "number" && Array.isArray(value.ids) ? value : null;
  } catch {
    return null;
  }
}

function receivedAgo(value: string | null) {
  if (!value) return "";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 60_000));
  if (minutes < 1) return "Just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hr ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? "Yesterday" : `${days} days ago`;
}

const dayFormat = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" });
const timeFormat = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });

const SUBTYPE_LABELS: Record<string, string> = {
  confirmation: "Confirmed",
  reminder: "Reminder",
  reschedule: "Rescheduled",
  time_change: "Time changed",
  cancellation: "Canceled",
};

function buildRows(pending: PendingInterview[], mail: MailAlert[]) {
  const unreadIds = new Set(mail.map((item) => item.email_id));
  const needs: Row[] = pending.map((item) => ({
    emailId: item.email_id,
    mailboxId: item.mailbox_id,
    account: item.email_account,
    kind: item.kind,
    subtype: null,
    company: item.company,
    role: item.role,
    subject: item.subject,
    receivedAt: item.received_at,
    unread: unreadIds.has(item.email_id),
    startAt: null,
    slots: item.slots,
    pending: true,
  }));
  const pendingIds = new Set(pending.map((item) => item.email_id));
  const scheduled: Row[] = [];
  for (const item of mail) {
    if (pendingIds.has(item.email_id)) continue;
    // Invitation rows belong in the attention modal only when the backend's
    // pending-interview resolver says they are still unresolved.  The unread
    // alerts feed is intentionally broader and can still contain the original
    // invitation after a later message/ICS books it (often in another thread).
    if (item.label === "interview_invitation") continue;
    const row: Row = {
      emailId: item.email_id,
      mailboxId: item.mailbox_id,
      account: item.email_account,
      kind: "scheduled",
      subtype: item.interview_subtype,
      company: item.company,
      role: item.role,
      subject: item.subject,
      receivedAt: item.received_at,
      unread: true,
      startAt: item.start_at,
      slots: [],
      pending: false,
    };
    (row.kind === "scheduled" ? scheduled : needs).push(row);
  }
  return { needs, scheduled };
}

function initials(row: Row) {
  const source = (row.company || row.subject || "?").replace(/[^\p{L}\p{N} ]/gu, " ").trim();
  const words = source.split(/\s+/).filter(Boolean);
  return ((words[0]?.[0] ?? "?") + (words[1]?.[0] ?? "")).toUpperCase();
}

function AttentionRow({ row, color, leaving, onOpen, onDiscard }: {
  row: Row;
  color: string;
  leaving: boolean;
  onOpen: (id: number) => void;
  onDiscard: (row: Row) => void;
}) {
  const start = row.startAt ? new Date(row.startAt) : null;
  const tag = row.kind === "reschedule"
    ? { label: "Reschedule", className: "is-reschedule" }
    : row.kind === "invitation"
      ? { label: "Invitation", className: "is-invite" }
      : { label: SUBTYPE_LABELS[row.subtype ?? ""] ?? "Scheduled", className: `is-${row.subtype ?? "scheduled"}` };
  return (
    <li className={`mail-alert-row${row.unread ? " is-unread" : ""}${leaving ? " is-leaving" : ""}`} style={{ "--account-color": color } as CSSProperties}>
      <span className="mail-alert-avatar" aria-hidden="true">{initials(row)}</span>
      <div className="mail-alert-copy">
        <div className="mail-alert-line">
          {row.unread && <span className="mail-alert-unread" role="img" aria-label="Unread" />}
          <strong title={row.company || row.subject}>{row.company || row.subject || "Interview email"}</strong>
          <span className={`mail-alert-tag ${tag.className}`}>{tag.label}</span>
        </div>
        <span className="mail-alert-role" title={row.role || row.subject}>{row.role || "Role not mentioned"}</span>
        <span className="mail-alert-meta">
          <span className="mail-alert-dot" aria-hidden="true" />
          <span className="mail-alert-account">{row.account}</span>
          {row.receivedAt && <span>· {receivedAgo(row.receivedAt)}</span>}
        </span>
        {row.kind !== "scheduled" && (
          row.slots.length > 0 ? (
            <span className="mail-alert-slots" aria-label="Proposed times">
              {row.slots.slice(0, 4).map((slot) => (
                <span key={slot.start_at}>{dayFormat.format(new Date(slot.start_at))} · {timeFormat.format(new Date(slot.start_at))}</span>
              ))}
              {row.slots.length > 4 && <span>+{row.slots.length - 4} more</span>}
            </span>
          ) : <span className="mail-alert-hint">No times proposed yet. Reply with your availability.</span>
        )}
      </div>
      {start && (
        <span className={`mail-alert-when${start.getTime() < Date.now() ? " is-past" : ""}`}>
          <small>{dayFormat.format(start)}</small>
          <strong>{timeFormat.format(start)}</strong>
        </span>
      )}
      <div className="mail-alert-actions">
        <button type="button" className="mail-alert-open" onClick={() => onOpen(row.emailId)}>
          {row.kind === "scheduled" ? "Open" : "Reply"}
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3.5 10.5 8 6 12.5" /></svg>
        </button>
        {row.pending && (
          <button
            type="button"
            className="mail-alert-discard"
            aria-label={`Discard ${row.company || row.subject || "interview"}: already booked or not needed`}
            title="Already booked or not needed? Discard it"
            onClick={() => onDiscard(row)}
          >
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.1a1 1 0 0 0 1 .9h3.8a1 1 0 0 0 1-.9l.6-8.1" /></svg>
            Discard
          </button>
        )}
      </div>
    </li>
  );
}

export default function InterviewEmailAlert({ mailboxes, refreshKey, suppressed, onOpenEmail, onOpenCalendar }: {
  mailboxes: Mailbox[];
  refreshKey: unknown;
  suppressed?: boolean;
  onOpenEmail: (id: number) => void;
  onOpenCalendar: () => void;
}) {
  const [pending, setPending] = useState<PendingInterview[]>([]);
  const [mail, setMail] = useState<MailAlert[]>([]);
  const [snooze, setSnooze] = useState<Snooze | null>(readSnooze);
  const [now, setNow] = useState(() => Date.now());
  const [closing, setClosing] = useState(false);
  const [reopened, setReopened] = useState(false);
  const [leaving, setLeaving] = useState<Set<number>>(() => new Set());
  const [hiddenIds, setHiddenIds] = useState<Set<number>>(() => new Set());
  const [undo, setUndo] = useState<{ emailId: number; name: string } | null>(null);
  const [discardError, setDiscardError] = useState<string | null>(null);
  const undoTimer = useRef<number | null>(null);
  const primaryRef = useRef<HTMLButtonElement>(null);
  const launcherRef = useRef<HTMLButtonElement>(null);

  const refresh = useCallback(async () => {
    const [nextPending, nextMail] = await Promise.allSettled([fetchPendingInterviews(), fetchInterviewEmailAlerts()]);
    if (nextPending.status === "fulfilled") setPending(nextPending.value);
    if (nextMail.status === "fulfilled") setMail(nextMail.value);
    setNow(Date.now());
  }, []);

  const debounceRef = useRef<number | null>(null);
  const scheduleRefresh = useCallback(() => {
    // A sync emits one event per message; coalesce them into one request.
    if (debounceRef.current != null) window.clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(() => {
      debounceRef.current = null;
      void refresh();
    }, 1200);
  }, [refresh]);

  useEffect(() => {
    // Let the inbox list load first; this check is the heaviest startup request.
    const first = window.setTimeout(() => { void refresh(); }, 1500);
    const timer = window.setInterval(() => { void refresh(); }, REFRESH_MS);
    const source = openEvents();
    const names = ["connected", "email.classified", "sync.done", "calendar.changed"];
    names.forEach((name) => source.addEventListener(name, scheduleRefresh));
    return () => {
      window.clearTimeout(first);
      window.clearInterval(timer);
      if (debounceRef.current != null) window.clearTimeout(debounceRef.current);
      names.forEach((name) => source.removeEventListener(name, scheduleRefresh));
      source.close();
    };
  }, [refresh, scheduleRefresh]);

  const seenRefreshKey = useRef(refreshKey);
  useEffect(() => {
    if (seenRefreshKey.current === refreshKey) return;
    seenRefreshKey.current = refreshKey;
    scheduleRefresh();
  }, [refreshKey, scheduleRefresh]);

  const { needs, scheduled } = useMemo(() => buildRows(
    pending.filter((item) => !hiddenIds.has(item.email_id)),
    mail.filter((item) => !hiddenIds.has(item.email_id)),
  ), [pending, mail, hiddenIds]);
  const rows = useMemo(() => [...needs, ...scheduled], [needs, scheduled]);
  const snoozed = snooze != null && snooze.until > now && rows.every((row) => snooze.ids.includes(row.emailId));
  // Stay open while an undo is offered, even if the last row was just discarded.
  const open = (rows.length > 0 || undo != null) && !suppressed && (reopened || !snoozed);
  const showLauncher = rows.length > 0 && !suppressed && !open;
  const colorFor = (mailboxId: number) => accountColor(mailboxes.find((mailbox) => mailbox.id === mailboxId) ?? { id: mailboxId });

  const close = useCallback((after?: () => void) => {
    setClosing(true);
    window.setTimeout(() => {
      const next = { until: Date.now() + SNOOZE_MS, ids: rows.map((row) => row.emailId) };
      try { sessionStorage.setItem(SNOOZE_KEY, JSON.stringify(next)); } catch { /* ignore unavailable storage */ }
      setSnooze(next);
      setReopened(false);
      setClosing(false);
      after?.();
      // Keyboard users land back on the launcher instead of the page top.
      if (!after) window.requestAnimationFrame(() => launcherRef.current?.focus({ preventScroll: true }));
    }, 170);
  }, [rows]);

  const clearUndo = useCallback(() => {
    if (undoTimer.current != null) window.clearTimeout(undoTimer.current);
    undoTimer.current = null;
    setUndo(null);
  }, []);
  useEffect(() => () => { if (undoTimer.current != null) window.clearTimeout(undoTimer.current); }, []);

  const discard = useCallback((row: Row) => {
    setDiscardError(null);
    setLeaving((current) => new Set(current).add(row.emailId));
    // Let the row slide out before the list closes the gap. Cancelled below
    // if the API call fails first — otherwise this still fires afterward
    // and re-hides a row the catch handler just restored, so a failed
    // discard looked like it had silently succeeded.
    const hideTimer = window.setTimeout(() => {
      setHiddenIds((current) => new Set(current).add(row.emailId));
      setLeaving((current) => { const next = new Set(current); next.delete(row.emailId); return next; });
    }, 220);
    if (undoTimer.current != null) window.clearTimeout(undoTimer.current);
    setUndo({ emailId: row.emailId, name: row.company || row.subject || "Interview" });
    undoTimer.current = window.setTimeout(() => { undoTimer.current = null; setUndo(null); }, UNDO_MS);
    setPendingInterviewDismissed(row.emailId, true).catch((err: unknown) => {
      window.clearTimeout(hideTimer);
      setLeaving((current) => { const next = new Set(current); next.delete(row.emailId); return next; });
      setHiddenIds((current) => { const next = new Set(current); next.delete(row.emailId); return next; });
      clearUndo();
      setDiscardError(err instanceof Error ? err.message : "Could not discard. Try again.");
    });
  }, [clearUndo]);

  const restore = useCallback(async () => {
    if (!undo) return;
    const { emailId } = undo;
    clearUndo();
    setHiddenIds((current) => { const next = new Set(current); next.delete(emailId); return next; });
    try {
      await setPendingInterviewDismissed(emailId, false);
      await refresh();
    } catch (err) {
      setHiddenIds((current) => new Set(current).add(emailId));
      setDiscardError(err instanceof Error ? err.message : "Could not restore. Try again.");
    }
  }, [undo, clearUndo, refresh]);

  useEffect(() => {
    if (!open) return;
    primaryRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopImmediatePropagation(); close(); }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [open, close]);

  const needsCount = needs.length;
  const unreadTotal = rows.filter((row) => row.unread).length;

  if (showLauncher) {
    const label = `${rows.length} interview${rows.length === 1 ? "" : "s"} need${rows.length === 1 ? "s" : ""} attention`;
    return createPortal(
      <button
        type="button"
        ref={launcherRef}
        className={`interview-launcher${needsCount ? " is-urgent" : ""}`}
        aria-label={`${label}. Open interview alerts`}
        aria-haspopup="dialog"
        onClick={() => setReopened(true)}
      >
        <span className="interview-launcher-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24"><rect x="3.5" y="5" width="17" height="15" rx="3" /><path d="M3.5 9.5h17M8 3v4M16 3v4M9 14.5l2 2 4-4" /></svg>
          <span className="interview-launcher-count">{rows.length > 99 ? "99+" : rows.length}</span>
        </span>
        <span className="interview-launcher-text" aria-hidden="true">
          <strong>Interviews</strong>
          <small>{needsCount ? `${needsCount} not booked` : `${unreadTotal} unread update${unreadTotal === 1 ? "" : "s"}`}</small>
        </span>
      </button>,
      document.body,
    );
  }

  if (!open) return null;

  const openEmail = (id: number) => {
    setMail((current) => current.filter((item) => item.email_id !== id));
    close(() => onOpenEmail(id));
  };
  const summary = [
    needsCount ? `${needsCount} not booked yet` : "",
    unreadTotal ? `${unreadTotal} unread interview email${unreadTotal === 1 ? "" : "s"}` : "",
  ].filter(Boolean).join(" · ");

  return createPortal(
    <div className={`mail-alert-backdrop${closing ? " is-closing" : ""}`} onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}>
      <div className="mail-alert" role="alertdialog" aria-modal="true" aria-labelledby="mail-alert-title" aria-describedby="mail-alert-summary">
        <div className="mail-alert-glow" aria-hidden="true" />
        <header className="mail-alert-head">
          <span className="mail-alert-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><rect x="3.5" y="5" width="17" height="15" rx="3" /><path d="M3.5 9.5h17M8 3v4M16 3v4M9 14.5l2 2 4-4" /></svg>
            <span className="mail-alert-count">{rows.length}</span>
          </span>
          <div>
            <p className="mail-alert-eyebrow">Time to schedule</p>
            <h2 id="mail-alert-title">{rows.length ? `${rows.length} interview${rows.length === 1 ? " needs" : "s need"} your attention` : "Nothing left to schedule"}</h2>
            <p id="mail-alert-summary">{summary}</p>
          </div>
          <button type="button" className="mail-alert-close" aria-label="Close" onClick={() => close()}>
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4.5 4.5 7 7m0-7-7 7" /></svg>
          </button>
        </header>
        <div className="mail-alert-body">
          {needs.length > 0 && (
            <section>
              <h3><span className="mail-alert-section-dot is-invite" />Not booked yet<span>{needs.length}</span></h3>
              <ul>{needs.map((row) => <AttentionRow key={row.emailId} row={row} color={colorFor(row.mailboxId)} leaving={leaving.has(row.emailId)} onOpen={openEmail} onDiscard={discard} />)}</ul>
            </section>
          )}
          {scheduled.length > 0 && (
            <section>
              <h3><span className="mail-alert-section-dot is-scheduled" />Unread scheduling updates<span>{scheduled.length}</span></h3>
              <ul>{scheduled.map((row) => <AttentionRow key={row.emailId} row={row} color={colorFor(row.mailboxId)} leaving={leaving.has(row.emailId)} onOpen={openEmail} onDiscard={discard} />)}</ul>
            </section>
          )}
          {rows.length === 0 && (
            <div className="mail-alert-empty">
              <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="m8 12.5 2.7 2.7L16.5 9.5" /></svg>
              <strong>All caught up</strong>
              <span>No interviews are waiting on you.</span>
            </div>
          )}
        </div>
        {discardError && <p className="mail-alert-error" role="alert">{discardError}</p>}
        <footer className="mail-alert-foot">
          {undo ? (
            <span className="mail-alert-undo" role="status">
              <span><strong>{undo.name}</strong> discarded</span>
              <button type="button" onClick={() => void restore()}>Undo</button>
            </span>
          ) : (
            <span>Reminds you again in 30 minutes, or sooner if something new arrives.</span>
          )}
          <button type="button" className="mail-alert-secondary" onClick={() => close()}>Remind me later</button>
          <button type="button" ref={primaryRef} className="mail-alert-primary" onClick={() => close(onOpenCalendar)}>
            <svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="3.5" width="11" height="10" rx="2" /><path d="M2.5 6.5h11M5.5 2v3M10.5 2v3" /></svg>
            Open calendar
          </button>
        </footer>
      </div>
    </div>,
    document.body,
  );
}
