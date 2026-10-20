import { CSSProperties, Dispatch, DragEvent, FormEvent, KeyboardEvent, PointerEvent, SetStateAction, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import ApplicationActionCopies from "./components/ApplicationActionCopies";
import EmailBody from "./components/EmailBody";
import CompactSelect from "./components/CompactSelect";
import ModernDateTimePicker from "./components/ModernDateTimePicker";
import MessageRow from "./components/MessageRow";
import CategoryPicker, { type CategoryChoice } from "./components/CategoryPicker";
import SystemAlert, { type SystemIssue } from "./components/SystemAlert";
import InterviewAlarm from "./components/InterviewAlarm";
import InterviewEmailAlert from "./components/InterviewEmailAlert";
import SettingsPage, { type SettingsSection } from "./pages/SettingsPage";
import CalendarPage, { warmCalendarCache } from "./pages/CalendarPage";
import MonitoringPage from "./pages/MonitoringPage";
import StatisticsPage from "./pages/StatisticsPage";
import {
  disconnectMailbox,
  openEvents,
  fetchAiStatus,
  fetchAutoSyncStatus,
  fetchClassifyPromptStatus,
  fetchEmailConversation,
  fetchEmailDetail,
  fetchEmails,
  peekEmailDetail,
  peekEmailDetailRequest,
  prefetchEmailDetail,
  createAiReply,
  fetchReclassifyStatus,
  sendEmail,
  fetchMailboxes,
  updateMailboxColor,
  markAllRead,
  oauthStartUrl,
  syncMailbox,
  reclassifyMailbox,
  stopSyncMailbox,
  stopReclassifyMailbox,
  updateClassifyPrompt,
  updateEmailLabel,
  type EmailDetail,
  type EmailItem,
  type EmailLabel,
  type InterviewSubtype,
  type EmailPage,
  type AiStatus,
  type AutoSyncStatus,
  type MailFolder,
  type Mailbox,
  type Provider,
  type SessionUser,
} from "./api";
import { accountColor } from "./accountColor";
import { CLASSIFY_LABELS, CLASSIFY_LABEL_TITLES, EMPTY_LABEL_COUNTS, INTERVIEW_SUBTYPE_TITLES } from "./labels";
import {
  applyFontFamily,
  applyFontSize,
  loadConversationView,
  loadFontFamily,
  loadFontSize,
  loadInboxType,
  loadReadingPane,
  loadViewMode,
  saveConversationView,
  saveFontFamily,
  saveFontSize,
  saveInboxType,
  saveReadingPane,
  saveViewMode,
  type FontFamily,
  type FontSize,
  type InboxType,
  type ReadingPane,
  type ViewMode,
} from "./prefs";
import { applyTheme, loadThemePref, saveThemePref, type ThemePref } from "./theme";

const PAGE_SIZE = 50;
const LIST_OVERSCAN = 12;
const LIST_ROW_GROUP_H = 40;
const LIST_ROW_EMAIL_H = 102;
const LIST_ROW_TABLE_H = 72;

type ListRow =
  | { kind: "group"; key: string; heading: string; count: number }
  | { kind: "email"; email: EmailItem };

const ACCOUNTS_MIN = 220;
const LIST_MIN = 240;
const READER_MIN = 280;
const LIST_HEIGHT_MIN = 180;
const READER_HEIGHT_MIN = 240;
const ACCOUNTS_W_KEY = "email-checker-accounts-w";
const SIDEBAR_COLLAPSED_KEY = "email-checker-sidebar-collapsed";
const LIST_W_KEY = "email-checker-list-w";
const LIST_H_KEY = "email-checker-list-h";
const ACCOUNTS_ORDER_KEY = "email-checker-accounts-order";

function loadStoredWidth(key: string, fallback: number, min: number): number {
  try {
    const n = Number(localStorage.getItem(key));
    if (Number.isFinite(n) && n >= min) return Math.round(n);
  } catch {
    /* ignore */
  }
  return fallback;
}

const MAIL_FOLDERS: MailFolder[] = ["inbox", "sent", "spam", "trash", "archive"];
const MAIL_FOLDER_TITLES: Record<MailFolder, string> = {
  inbox: "Inbox",
  sent: "Sent",
  spam: "Spam",
  trash: "Trash",
  archive: "Archive",
};
type CountCell = { mailbox_id: number; label: string; folder: string; unread: boolean; count: number };
type CountTally = { total: number; unread: number };

function adjustCountCell(cells: CountCell[], key: Omit<CountCell, "count">, delta: number): CountCell[] {
  const index = cells.findIndex((cell) =>
    cell.mailbox_id === key.mailbox_id && cell.label === key.label && cell.folder === key.folder && cell.unread === key.unread,
  );
  if (index < 0) {
    return delta > 0 ? [...cells, { ...key, count: delta }] : cells;
  }
  const next = [...cells];
  next[index] = { ...cells[index], count: Math.max(0, cells[index].count + delta) };
  return next;
}

function tallyCounts(cells: CountCell[], match: (cell: CountCell) => boolean): CountTally {
  const tally = { total: 0, unread: 0 };
  for (const cell of cells) {
    if (!match(cell)) continue;
    tally.total += cell.count;
    if (cell.unread) tally.unread += cell.count;
  }
  return tally;
}

function mailFolderOf(email: { folder?: MailFolder | string | null }): MailFolder {
  const value = email.folder;
  return MAIL_FOLDERS.includes(value as MailFolder) ? (value as MailFolder) : "inbox";
}

function parseLabelCounts(raw: Record<string, number>): Record<EmailLabel, number> {
  const counts = { ...EMPTY_LABEL_COUNTS };
  for (const key of CLASSIFY_LABELS) {
    counts[key] = raw[key] ?? 0;
  }
  return counts;
}

function parseMailboxCounts(raw: Record<string, number>): Record<number, number> {
  const out: Record<number, number> = {};
  for (const [key, value] of Object.entries(raw)) {
    const id = Number(key);
    if (!Number.isNaN(id)) out[id] = value;
  }
  return out;
}

function parseApiDate(value: string | null): Date | null {
  if (!value) return null;
  // Normalize "2026-09-21 13:15:05+00:00" (Python str) → ISO with T
  let normalized = value.trim().replace(" ", "T");
  const hasZone = /Z$/i.test(normalized) || /[+-]\d{2}:?\d{2}$/.test(normalized);
  if (!hasZone) normalized = `${normalized}Z`;
  const d = new Date(normalized);
  return Number.isNaN(d.getTime()) ? null : d;
}

function localDateKey(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function localDateTimeValue(d: Date): string {
  const hour = String(d.getHours()).padStart(2, "0");
  const minute = String(d.getMinutes()).padStart(2, "0");
  return `${localDateKey(d)}T${hour}:${minute}`;
}

function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

function formatDateHeading(key: string): string {
  const [y, m, d] = key.split("-").map(Number);
  const date = new Date(y, m - 1, d);
  const today = startOfDay(new Date());
  const diffDays = Math.round((today.getTime() - startOfDay(date).getTime()) / 86_400_000);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  return new Intl.DateTimeFormat(undefined, {
    weekday: "long",
    month: "short",
    day: "numeric",
  }).format(date);
}

function formatItemTimeAndDay(value: string | null): { time: string; day: string } {
  const d = parseApiDate(value);
  if (!d) return { time: "", day: "" };
  return {
    time: new Intl.DateTimeFormat(undefined, {
      hour: "numeric",
      minute: "2-digit",
    }).format(d),
    day: new Intl.DateTimeFormat(undefined, { weekday: "short" }).format(d),
  };
}

function formatReaderTime(value: string | null): string {
  const d = parseApiDate(value);
  if (!d) return "";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(d);
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

function emailRowHeight(mode: ViewMode): number {
  if (mode === "table") return LIST_ROW_TABLE_H;
  return LIST_ROW_EMAIL_H;
}

function receivedTime(email: EmailItem): number {
  return parseApiDate(email.received_at)?.getTime() ?? 0;
}

function sortEmailsForInbox(items: EmailItem[], inboxType: InboxType): EmailItem[] {
  if (inboxType !== "unread_first") return items;
  return [...items].sort((a, b) => {
    const ar = a.is_read ? 1 : 0;
    const br = b.is_read ? 1 : 0;
    if (ar !== br) return ar - br;
    const at = receivedTime(a);
    const bt = receivedTime(b);
    if (at !== bt) return bt - at;
    return b.id - a.id;
  });
}

function groupEmailsByDate(items: EmailItem[]): { key: string; heading: string; items: EmailItem[] }[] {
  const buckets = new Map<string, EmailItem[]>();
  for (const email of items) {
    let key = "unknown";
    if (email.received_at) {
      try {
        const received = parseApiDate(email.received_at);
        if (received) key = localDateKey(received);
      } catch {
        key = "unknown";
      }
    }
    const list = buckets.get(key);
    if (list) list.push(email);
    else buckets.set(key, [email]);
  }
  return [...buckets.entries()]
    .sort(([a], [b]) => (a === "unknown" ? 1 : b === "unknown" ? -1 : b.localeCompare(a)))
    .map(([key, groupItems]) => ({
      key,
      heading: key === "unknown" ? "Unknown date" : formatDateHeading(key),
      items: groupItems,
    }));
}

function providerMark(provider: Provider): string {
  return provider === "google" ? "G" : "O";
}

type SidebarIconName = "home" | "inbox" | "calendar" | "monitoring" | "statistics" | "categories" | "sent" | "spam" | "trash" | "archive" | "settings";

function SidebarIcon({ name }: { name: SidebarIconName }) {
  const path = name === "home" ? <><path d="m3 10 9-7 9 7" /><path d="M5 9v11h14V9M9 20v-7h6v7" /></>
    : name === "inbox" ? <><rect x="3" y="4" width="18" height="16" rx="3" /><path d="M3 14h5l2 3h4l2-3h5M8 8h8" /></>
    : name === "calendar" ? <><rect x="3" y="4.5" width="18" height="16" rx="3" /><path d="M7 2.5v4M17 2.5v4M3 9h18" /><path d="M8 13h3v3H8z" /></>
    : name === "monitoring" ? <><path d="M3 12h3.5l2-6 3.5 12 2.5-7.5L18.5 12H21" /><rect x="2.5" y="4" width="19" height="16" rx="3.5" /></>
    : name === "statistics" ? <><path d="M21 21H4a1 1 0 0 1-1-1V3" /><path d="M7 15l3.5-4 3 2.5L20 6" /><circle cx="20" cy="6" r="1.4" fill="currentColor" stroke="none" /></>
    : name === "categories" ? <><path d="M12 3v6M8 9h8M9 9l-4 11M15 9l4 11M4 20h16" /></>
    : name === "sent" ? <><path d="m3 11 18-8-7 18-3.5-7.5L3 11Z" /><path d="m10.5 13.5 4-4" /></>
    : name === "spam" ? <><path d="M8 3h8l5 5v8l-5 5H8l-5-5V8l5-5Z" /><path d="M12 7v6M12 17h.01" /></>
    : name === "trash" ? <><path d="M4 7h16M9 3h6l1 4H8l1-4ZM7 7l1 14h8l1-14M10 11v6M14 11v6" /></>
    : name === "archive" ? <><path d="M4 4h16l-2 4H6L4 4Z" /><path d="M6 8v12h12V8M9 12h6" /></>
    : <><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M4.93 4.93l2.12 2.12M16.95 16.95l2.12 2.12M2 12h3M19 12h3M4.93 19.07l2.12-2.12M16.95 7.05l2.12-2.12" /></>;
  return <svg className="sidebar-nav-icon" viewBox="0 0 24 24" aria-hidden="true">{path}</svg>;
}

export default function App({ sessionUser, onSignOut }: { sessionUser: SessionUser; onSignOut: () => void }) {
  const [activePage, setActivePage] = useState<"inbox" | "calendar" | "monitoring" | "statistics">(
    sessionUser.is_admin ? "monitoring" : "inbox",
  );
  const [calendarMounted, setCalendarMounted] = useState(false);
  const [mailboxes, setMailboxes] = useState<Mailbox[]>([]);
  const [accountOrder, setAccountOrder] = useState<number[]>(() => {
    try { return JSON.parse(localStorage.getItem(ACCOUNTS_ORDER_KEY) ?? "[]"); } catch { return []; }
  });
  const [draggedAccountId, setDraggedAccountId] = useState<number | null>(null);
  const [emails, setEmails] = useState<EmailItem[]>([]);
  const [selectedMailboxId, setSelectedMailboxId] = useState<number | null>(null);
  const [label, setLabel] = useState<"all" | EmailLabel>("all");
  const [interviewSubtype, setInterviewSubtype] = useState<"all" | InterviewSubtype>("all");
  const [folder, setFolder] = useState<"all" | MailFolder>("all");
  const [searchInput, setSearchInput] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [filteredTotal, setFilteredTotal] = useState(0);
  const [nextCursor, setNextCursor] = useState<number | null>(null);
  const [nextReceivedAt, setNextReceivedAt] = useState<string | null>(null);
  const [nextIsRead, setNextIsRead] = useState<boolean | null>(null);
  const [labelCounts, setLabelCounts] = useState<Record<EmailLabel, number>>(EMPTY_LABEL_COUNTS);
  const [mailboxUnreadCounts, setMailboxUnreadCounts] = useState<Record<number, number>>({});
  const [countCells, setCountCells] = useState<CountCell[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [aiStatus, setAiStatus] = useState<AiStatus | null>(null);
  const [aiStatusOffline, setAiStatusOffline] = useState(false);
  const [autoSyncStatus, setAutoSyncStatus] = useState<AutoSyncStatus | null>(null);
  const [autoSyncOffline, setAutoSyncOffline] = useState(false);
  const aiStatusFailCountRef = useRef(0);
  const autoSyncFailCountRef = useRef(0);
  const [banner, setBanner] = useState<string | null>(null);
  const [stableIssueKey, setStableIssueKey] = useState("");
  const [dismissedIssueKey, setDismissedIssueKey] = useState(
    () => sessionStorage.getItem("systemAlertDismissed") ?? "",
  );
  const [recheckingStatus, setRecheckingStatus] = useState(false);

  const [connectEmail, setConnectEmail] = useState("");
  const [connectProvider, setConnectProvider] = useState<Provider>("google");
  const [addAccountOpen, setAddAccountOpen] = useState(false);
  const [dialogStep, setDialogStep] = useState<"type" | "email">("type");

  const [syncingIds, setSyncingIds] = useState<Set<number>>(() => new Set());
  const [reclassifyingIds, setReclassifyingIds] = useState<Set<number>>(() => new Set());
  const [reclassifyProgress, setReclassifyProgress] = useState<Record<number, { processed: number; total: number; failed: number }>>({});
  const [mailboxActions, setMailboxActions] = useState<Record<number, {
    type: "sync" | "reclassify" | "success" | "error";
    message: string;
  }>>({});
  const [saveTraining, setSaveTraining] = useState(false);
  const [savingLabel, setSavingLabel] = useState(false);
  const [unusedTraining, setUnusedTraining] = useState(0);
  const [updatingPrompt, setUpdatingPrompt] = useState(false);
  const [pendingLabel, setPendingLabel] = useState<{ emailId: number; nextLabel: EmailLabel } | null>(
    null
  );
  const [pendingSubtype, setPendingSubtype] = useState<InterviewSubtype | "">("");
  const [categoryPickerOpen, setCategoryPickerOpen] = useState(false);
  const [stickyUnreadId, setStickyUnreadId] = useState<number | null>(null);
  const [markAllOpen, setMarkAllOpen] = useState(false);
  const [markingAllRead, setMarkingAllRead] = useState(false);
  const [markAllReadProgress, setMarkAllReadProgress] = useState<{ marked: number; total: number } | null>(null);

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [selected, setSelected] = useState<EmailDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [tableDialogOpen, setTableDialogOpen] = useState(false);
  const [syncMenuOpen, setSyncMenuOpen] = useState(false);
  const [calendarRefreshKey, setCalendarRefreshKey] = useState(0);
  const [collapsedDates, setCollapsedDates] = useState<Set<string>>(new Set());
  const [accountsW, setAccountsW] = useState(() => loadStoredWidth(ACCOUNTS_W_KEY, 300, ACCOUNTS_MIN));
  const [listW, setListW] = useState(() => loadStoredWidth(LIST_W_KEY, 360, LIST_MIN));
  const [listH, setListH] = useState(() =>
    loadStoredWidth(LIST_H_KEY, Math.max(LIST_HEIGHT_MIN, Math.round((window.innerHeight - 6) / 2)), LIST_HEIGHT_MIN)
  );
  const [themePref, setThemePref] = useState<ThemePref>(() => loadThemePref());
  const [viewMode, setViewMode] = useState<ViewMode>(() => loadViewMode());
  const [inboxType, setInboxType] = useState<InboxType>(() => loadInboxType());
  const [readingPane, setReadingPane] = useState<ReadingPane>(() => loadReadingPane());
  const [conversationView, setConversationView] = useState(() => loadConversationView());
  const [threadMessages, setThreadMessages] = useState<EmailDetail[]>([]);
  const [openThreadIds, setOpenThreadIds] = useState<number[]>([]);
  const [fontFamily, setFontFamily] = useState<FontFamily>(() => loadFontFamily());
  const [fontSize, setFontSize] = useState<FontSize>(() => loadFontSize());
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("appearance");
  const [sidebarSections, setSidebarSections] = useState({ inbox: true, categories: true });
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try { return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1"; } catch { return false; }
  });
  const [resizing, setResizing] = useState<"accounts" | "list" | null>(null);
  const [replyOpen, setReplyOpen] = useState(false);
  const [replyText, setReplyText] = useState("");
  const [replyInstruction, setReplyInstruction] = useState("");
  const [replyFiles, setReplyFiles] = useState<File[]>([]);
  const [scheduleAt, setScheduleAt] = useState("");
  const [sendingReply, setSendingReply] = useState(false);
  const [aiDrafting, setAiDrafting] = useState(false);
  const [scheduleDialogOpen, setScheduleDialogOpen] = useState(false);

  const filterRef = useRef({
    mailboxId: selectedMailboxId,
    label,
    interviewSubtype,
    folder,
    query: searchQuery,
    inboxType,
    conversationView,
  });
  filterRef.current = {
    mailboxId: selectedMailboxId,
    label,
    interviewSubtype,
    folder,
    query: searchQuery,
    inboxType,
    conversationView,
  };
  const fetchGen = useRef(0);
  const firstPageAbortRef = useRef<AbortController | null>(null);
  const detailGen = useRef(0);
  const loadingMoreRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const readerDialogRef = useRef<HTMLElement>(null);
  const conversationScrollRef = useRef<HTMLDivElement>(null);
  const readerCloseRef = useRef<HTMLButtonElement>(null);
  const syncMenuRef = useRef<HTMLDivElement>(null);
  const didMountFilters = useRef(false);
  // Mailbox navigation starts its own load so the list (including date-group
  // counts) is refreshed from the clicked scope immediately.
  const skipNextMailboxFilterLoad = useRef(false);
  const shellRef = useRef<HTMLDivElement>(null);
  const accountsPaneRef = useRef<HTMLElement>(null);
  const dragRef = useRef<{
    which: "accounts" | "list";
    axis: "x" | "y";
    startX: number;
    startY: number;
    startAccounts: number;
    startList: number;
    startListHeight: number;
  } | null>(null);
  const paneSizesRef = useRef({ accounts: accountsW, list: listW, listHeight: listH });
  paneSizesRef.current = { accounts: accountsW, list: listW, listHeight: listH };
  const mailboxesRef = useRef(mailboxes);
  mailboxesRef.current = mailboxes;
  const syncingIdsRef = useRef(syncingIds);
  syncingIdsRef.current = syncingIds;
  const reclassifyingIdsRef = useRef(reclassifyingIds);
  reclassifyingIdsRef.current = reclassifyingIds;

  function accountName(id?: number | null): string {
    if (id == null) return "";
    return mailboxesRef.current.find((box) => box.id === id)?.email_address ?? "";
  }

  function withAccount(id: number | null | undefined, message: string): string {
    const name = accountName(id);
    return name ? `${name} — ${message}` : message;
  }

  function setMailboxAction(id: number | null | undefined, type: "sync" | "reclassify" | "success" | "error", message: string) {
    if (id == null) return;
    setMailboxActions((prev) => ({ ...prev, [id]: { type, message: withAccount(id, message) } }));
  }

  function setIdInSet(
    setter: Dispatch<SetStateAction<Set<number>>>,
    id: number,
    present: boolean,
  ) {
    setter((prev) => {
      const next = new Set(prev);
      if (present) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  const applyPageMeta = useCallback((page: EmailPage) => {
    setFilteredTotal(page.total);
    setHasMore(page.has_more);
    setNextCursor(page.next_cursor);
    setNextReceivedAt(page.next_received_at ?? null);
    setNextIsRead(page.next_is_read ?? null);
    setLabelCounts(parseLabelCounts(page.label_counts));
    setMailboxUnreadCounts(parseMailboxCounts(page.mailbox_unread_counts ?? {}));
    if (page.count_breakdown) {
      setCountCells(page.count_breakdown.map(([mailbox_id, label, folder, unread, count]) => ({ mailbox_id, label, folder, unread, count })));
    }
  }, []);

  const countRefreshTimer = useRef(0);
  const refreshCountsRef = useRef<() => void>(() => {});
  refreshCountsRef.current = () => {
    window.clearTimeout(countRefreshTimer.current);
    countRefreshTimer.current = window.setTimeout(() => {
      const {
        mailboxId,
        label: currentLabel,
        interviewSubtype: currentSubtype,
        query,
        inboxType: currentInboxType,
      } = filterRef.current;
      void fetchEmails({
        mailboxId,
        label: currentLabel,
        interviewSubtype: currentSubtype === "all" ? null : currentSubtype,
        folder: "all",
        query,
        inboxType: currentInboxType,
        limit: 1,
      }).then((page) => {
        setLabelCounts(parseLabelCounts(page.label_counts));
        setMailboxUnreadCounts(parseMailboxCounts(page.mailbox_unread_counts ?? {}));
        if (page.count_breakdown) {
          setCountCells(page.count_breakdown.map(([mailbox_id, label, folder, unread, count]) => ({ mailbox_id, label, folder, unread, count })));
        }
      }).catch(() => {});
    }, 800);
  };

  const loadFirstPage = useCallback(async () => {
    firstPageAbortRef.current?.abort();
    const controller = new AbortController();
    firstPageAbortRef.current = controller;
    const gen = ++fetchGen.current;
    const {
      mailboxId,
      label: currentLabel,
      interviewSubtype: currentSubtype,
      query,
      inboxType: currentInboxType,
      conversationView: currentConversation,
    } = filterRef.current;
    setLoading(true);
    setLoadingMore(false);
    loadingMoreRef.current = false;
    setError(null);
    setHasMore(false);
    setFilteredTotal(0);
    setNextCursor(null);
    setNextReceivedAt(null);
    setNextIsRead(null);
    try {
      const page = await fetchEmails({
        mailboxId,
        label: currentLabel,
        interviewSubtype: currentSubtype === "all" ? null : currentSubtype,
        folder: "all",
        query,
        inboxType: currentInboxType,
        conversation: currentConversation,
        limit: PAGE_SIZE,
        signal: controller.signal,
      });
      if (gen !== fetchGen.current) return;
      setEmails(sortEmailsForInbox(page.items, currentInboxType));
      applyPageMeta(page);
    } catch (err) {
      if (controller.signal.aborted) return;
      if (gen !== fetchGen.current) return;
      setError(err instanceof Error ? err.message : "Failed to load emails");
      setEmails([]);
      setFilteredTotal(0);
    } finally {
      if (gen === fetchGen.current) setLoading(false);
      if (firstPageAbortRef.current === controller) firstPageAbortRef.current = null;
    }
  }, [applyPageMeta]);

  useEffect(() => () => firstPageAbortRef.current?.abort(), []);

  const loadMore = useCallback(async () => {
    if (loadingMoreRef.current || !hasMore || nextCursor == null) return;
    loadingMoreRef.current = true;
    setLoadingMore(true);
    const gen = fetchGen.current;
    const {
      mailboxId,
      label: currentLabel,
      interviewSubtype: currentSubtype,
      query,
      inboxType: currentInboxType,
      conversationView: currentConversation,
    } = filterRef.current;
    try {
      const page = await fetchEmails({
        mailboxId,
        label: currentLabel,
        interviewSubtype: currentSubtype === "all" ? null : currentSubtype,
        folder: "all",
        query,
        inboxType: currentInboxType,
        conversation: currentConversation,
        limit: PAGE_SIZE,
        beforeId: nextCursor,
        beforeReceivedAt: nextReceivedAt,
        beforeIsRead: currentInboxType === "unread_first" ? nextIsRead : null,
      });
      if (gen !== fetchGen.current) return;
      setEmails((prev) => {
        const seen = new Set(prev.map((item) => item.id));
        const extra = page.items.filter((item) => !seen.has(item.id));
        if (!extra.length) return prev;
        return sortEmailsForInbox([...prev, ...extra], currentInboxType);
      });
      // Keep live badge totals from the first page and SSE/read updates. A
      // later page can carry a cached snapshot from before a message was read.
      setHasMore(page.has_more);
      setNextCursor(page.next_cursor);
      setNextReceivedAt(page.next_received_at ?? null);
      setNextIsRead(page.next_is_read ?? null);
    } catch (err) {
      if (gen !== fetchGen.current) return;
      setError(err instanceof Error ? err.message : "Failed to load emails");
    } finally {
      loadingMoreRef.current = false;
      if (gen === fetchGen.current) setLoadingMore(false);
    }
  }, [applyPageMeta, hasMore, nextCursor, nextReceivedAt, nextIsRead]);

  const refreshPromptStatus = useCallback(async () => {
    try {
      const status = await fetchClassifyPromptStatus();
      setUnusedTraining(status.unused_count);
    } catch {
      /* ignore */
    }
  }, []);

  // Sync completion only needs fresh mailbox metadata. Classification
  // completion also reloads the first page below: SSE delivery is best-effort,
  // so relying only on email.classified can leave a pre-classification row in
  // browser memory if that individual event was missed.
  const refreshMailboxes = useCallback(async () => {
    try {
      const boxes = await fetchMailboxes();
      setMailboxes(boxes);
    } catch {
      /* ignore */
    }
  }, []);

  const mailboxIdsKey = useMemo(() => mailboxes.map((mailbox) => mailbox.id).join(","), [mailboxes]);
  const refreshReclassifyStatuses = useCallback(async () => {
    if (!mailboxIdsKey) return;
    const ids = mailboxIdsKey.split(",").map(Number);
    const results = await Promise.allSettled(ids.map((id) => fetchReclassifyStatus(id)));
    const running = new Set<number>();
    const progress: Record<number, { processed: number; total: number; failed: number }> = {};
    results.forEach((result) => {
      if (result.status !== "fulfilled") return;
      const status = result.value;
      if (status.state !== "running") return;
      running.add(status.mailbox_id);
      progress[status.mailbox_id] = { processed: status.processed, total: status.total, failed: status.failed };
      setMailboxAction(status.mailbox_id, "reclassify", status.message || "Reclassifying in the background…");
    });
    reclassifyingIdsRef.current = running;
    setReclassifyingIds(running);
    setReclassifyProgress((previous) => ({ ...previous, ...progress }));
  }, [mailboxIdsKey]);

  const load = useCallback(async () => {
    setError(null);
    try {
      const boxes = await fetchMailboxes();
      setMailboxes(boxes);
      const current = filterRef.current.mailboxId;
      const nextId = current != null && !boxes.some((b) => b.id === current) ? null : current;
      filterRef.current = { ...filterRef.current, mailboxId: nextId };
      setSelectedMailboxId(nextId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load mailboxes");
      setLoading(false);
      return;
    }
    await Promise.all([loadFirstPage(), refreshPromptStatus()]);
  }, [loadFirstPage, refreshPromptStatus]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const oauth = params.get("oauth");
    if (oauth === "ok") {
      const name = params.get("name");
      const email = params.get("email");
      const account = name ? `${name} <${email}>` : email;
      const webhook = params.get("webhook");
      const webhookDetail = params.get("detail");
      setBanner(webhook === "fallback"
        ? `Connected ${params.get("provider") ?? "mailbox"}: ${account}. Webhook setup failed; polling is active. ${webhookDetail ?? ""}`.trim()
        : `Connected ${params.get("provider") ?? "mailbox"}: ${account}. Webhook sync is active.`
      );
      window.history.replaceState({}, "", "/");
    } else if (oauth === "error") {
      setBanner(`OAuth error: ${params.get("detail") ?? "unknown"}`);
      window.history.replaceState({}, "", "/");
    }
  }, []);

  useEffect(() => {
    const handle = window.setTimeout(() => setSearchQuery(searchInput.trim()), 300);
    return () => window.clearTimeout(handle);
  }, [searchInput]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!mailboxIdsKey) return;
    const handle = window.setTimeout(() => warmCalendarCache(), 700);
    return () => window.clearTimeout(handle);
  }, [mailboxIdsKey]);

  const refreshAiStatus = useCallback(async () => {
    try {
      setAiStatus(await fetchAiStatus());
      aiStatusFailCountRef.current = 0;
      setAiStatusOffline(false);
    } catch {
      aiStatusFailCountRef.current += 1;
      // One blip while classifying mail is normal; only mark offline after repeats.
      if (aiStatusFailCountRef.current >= 2) setAiStatusOffline(true);
    }
  }, []);

  useEffect(() => {
    void refreshAiStatus();
    const timer = window.setInterval(() => void refreshAiStatus(), 30_000);
    return () => window.clearInterval(timer);
  }, [refreshAiStatus]);

  const refreshAutoSyncStatus = useCallback(async () => {
    try {
      const status = await fetchAutoSyncStatus();
      setAutoSyncStatus(status);
      autoSyncFailCountRef.current = 0;
      setAutoSyncOffline(false);
      // The backend's per-mailbox state is authoritative. A dropped/reconnected
      // SSE stream can lose the sync.done event that would otherwise clear
      // syncingIds, leaving an account stuck showing "Syncing..." forever even
      // though the server has long since finished. Reconcile on every poll.
      if (syncingIdsRef.current.size > 0) {
        const stillRunning = new Set(
          (status.mailboxes ?? []).filter((mailbox) => mailbox.state === "running").map((mailbox) => mailbox.mailbox_id),
        );
        const next = new Set([...syncingIdsRef.current].filter((id) => stillRunning.has(id)));
        if (next.size !== syncingIdsRef.current.size) {
          syncingIdsRef.current = next;
          setSyncingIds(next);
        }
      }
    } catch {
      autoSyncFailCountRef.current += 1;
      if (autoSyncFailCountRef.current >= 2) setAutoSyncOffline(true);
    }
  }, []);

  useEffect(() => {
    void refreshAutoSyncStatus();
    const timer = window.setInterval(() => void refreshAutoSyncStatus(), 15_000);
    return () => window.clearInterval(timer);
  }, [refreshAutoSyncStatus]);

  useEffect(() => {
    void refreshReclassifyStatuses();
    const timer = window.setInterval(() => void refreshReclassifyStatuses(), 20_000);
    return () => window.clearInterval(timer);
  }, [refreshReclassifyStatuses]);

  useEffect(() => {
    refreshCountsRef.current();
  }, []);

  useEffect(() => {
    if (filterRef.current.folder === "all") return;
    filterRef.current = { ...filterRef.current, folder: "all" };
    setFolder("all");
  }, []);

  useEffect(() => {
    if (!didMountFilters.current) {
      didMountFilters.current = true;
      return;
    }
    if (skipNextMailboxFilterLoad.current) {
      skipNextMailboxFilterLoad.current = false;
      return;
    }
    setEmails([]);
    scrollRef.current?.scrollTo(0, 0);
    void loadFirstPage();
  }, [selectedMailboxId, label, interviewSubtype, folder, searchQuery, loadFirstPage]);

  useEffect(() => {
    const source = openEvents();
    let reconnecting = false;
    source.addEventListener("connected", () => {
      // The mount effect already loaded; only refetch what a dropped stream missed.
      if (reconnecting) void load();
      reconnecting = true;
    });
    source.addEventListener("mailbox.disconnected", (evt) => {
      try {
        const data = JSON.parse((evt as MessageEvent).data) as { mailbox_id?: number };
        const id = data.mailbox_id;
        if (id != null) {
          setEmails((prev) => prev.filter((e) => e.mailbox_id !== id));
          setSelected((prev) => (prev && prev.mailbox_id === id ? null : prev));
        }
      } catch {
        /* ignore */
      }
      void load();
    });
    source.addEventListener("email.classified", (evt) => {
      try {
        const item = JSON.parse((evt as MessageEvent).data) as EmailItem & {
          previous_label?: EmailLabel;
          updated?: boolean;
          classification_completed?: boolean;
        };
        const { mailboxId, label: currentLabel, interviewSubtype: currentSubtype, query } = filterRef.current;
        const matchesMailbox = mailboxId == null || item.mailbox_id === mailboxId;
        const matchesLabel = currentLabel === "all" || item.label === currentLabel;
        const matchesSubtype = currentSubtype === "all" || item.interview_subtype === currentSubtype;
        const itemFolder = mailFolderOf(item);
        const needle = query.trim().toLowerCase();
        const matchesQuery =
          !needle ||
          `${item.subject}\n${item.sender}\n${item.snippet}`.toLowerCase().includes(needle);
        const isUpdate = Boolean(item.updated);

        setEmails((prev) => {
          const inbox = filterRef.current.inboxType;
          const conversation = filterRef.current.conversationView;
          const matches = matchesMailbox && matchesLabel && matchesSubtype && matchesQuery;
          const threadIdx = conversation && item.thread_id
            ? prev.findIndex((email) => email.mailbox_id === item.mailbox_id && email.thread_id === item.thread_id)
            : -1;
          const existingIdx = threadIdx >= 0 ? threadIdx : prev.findIndex((email) => email.id === item.id);
          if (existingIdx >= 0) {
            const current = prev[existingIdx];
            const sameMessage = current.id === item.id;
            if (!sameMessage && isUpdate) return prev;
            if (!matches) return sameMessage ? prev.filter((email) => email.id !== item.id) : prev;
            const newer = (item.received_at ?? "") >= (current.received_at ?? "");
            const next = [...prev];
            if (!sameMessage && !newer) {
              next[existingIdx] = {
                ...current,
                thread_count: (current.thread_count ?? 1) + 1,
                is_read: current.is_read && Boolean(item.is_read),
              };
            } else {
              next[existingIdx] = {
                ...current,
                ...item,
                thread_count: sameMessage ? (current.thread_count ?? 1) : (current.thread_count ?? 1) + 1,
                is_read: sameMessage
                  ? Boolean(item.is_read ?? current.is_read)
                  : Boolean(item.is_read) && current.is_read,
              };
            }
            return sortEmailsForInbox(next, inbox);
          }
          if ((!isUpdate || item.classification_completed) && matches) {
            return sortEmailsForInbox([{ ...item, is_read: Boolean(item.is_read) }, ...prev], inbox);
          }
          return prev;
        });

        setSelected((prev) =>
          prev && prev.id === item.id
            ? { ...prev, label: item.label, interview_subtype: item.interview_subtype, confidence: item.confidence }
            : prev,
        );

        if (isUpdate) {
          const labelChanged = Boolean(item.previous_label && item.previous_label !== item.label);
          if (matchesMailbox && labelChanged) {
            setLabelCounts((prev) => {
              const next = { ...prev };
              next[item.previous_label!] = Math.max(0, (next[item.previous_label!] ?? 1) - 1);
              next[item.label] = (next[item.label] ?? 0) + 1;
              return next;
            });
          }
          if (labelChanged) {
            const fromLabel = item.previous_label!;
            const unread = item.is_read === false;
            setCountCells((prev) => adjustCountCell(
              adjustCountCell(prev, { mailbox_id: item.mailbox_id, label: fromLabel, folder: itemFolder, unread }, -1),
              { mailbox_id: item.mailbox_id, label: item.label, folder: itemFolder, unread },
              1,
            ));
          }
          return;
        }

        setCountCells((prev) => adjustCountCell(prev, { mailbox_id: item.mailbox_id, label: item.label, folder: itemFolder, unread: item.is_read === false }, 1));

        if (item.is_read === false) {
          setMailboxUnreadCounts((prev) => ({
            ...prev,
            [item.mailbox_id]: (prev[item.mailbox_id] ?? 0) + 1,
          }));
        }
        if (matchesMailbox) {
          setLabelCounts((prev) => ({
            ...prev,
            [item.label]: (prev[item.label] ?? 0) + 1,
          }));
        }
        refreshCountsRef.current();
      } catch {
        /* ignore */
      }
    });
    source.addEventListener("sync.progress", (evt) => {
      try {
        const data = JSON.parse((evt as MessageEvent).data) as {
          mailbox_id?: number;
          message?: string;
          imported?: number;
          total?: number;
        };
        if (data.mailbox_id != null) {
          setIdInSet(setSyncingIds, data.mailbox_id, true);
          syncingIdsRef.current = new Set(syncingIdsRef.current).add(data.mailbox_id);
        }
        const imported = data.imported ?? 0;
        const total = data.total ?? 0;
        setMailboxAction(data.mailbox_id, "sync", data.message || `Syncing… ${imported}${total ? ` / ${total}` : ""}`);
      } catch {
        /* ignore */
      }
    });
    source.addEventListener("reclassify.progress", (evt) => {
      try {
        const data = JSON.parse((evt as MessageEvent).data) as {
          mailbox_id?: number;
          message?: string;
          updated?: number;
          processed?: number;
          failed?: number;
          total?: number;
        };
        if (data.mailbox_id != null) {
          setIdInSet(setReclassifyingIds, data.mailbox_id, true);
          reclassifyingIdsRef.current = new Set(reclassifyingIdsRef.current).add(data.mailbox_id);
        }
        const updated = data.updated ?? 0;
        if (data.mailbox_id != null) {
          setReclassifyProgress((prev) => ({ ...prev, [data.mailbox_id!]: {
            processed: data.processed ?? 0, total: data.total ?? 0, failed: data.failed ?? 0,
          }}));
        }
        const total = data.total ?? 0;
        setMailboxAction(data.mailbox_id, "reclassify", data.message || `Reclassifying… ${updated}${total ? ` / ${total}` : ""}`);
      } catch {
        /* ignore */
      }
    });
    source.addEventListener("mark_all_read.progress", (evt) => {
      try {
        const data = JSON.parse((evt as MessageEvent).data) as { marked?: number; total?: number };
        setMarkAllReadProgress({ marked: data.marked ?? 0, total: data.total ?? 0 });
      } catch {
        /* ignore */
      }
    });
    source.addEventListener("mark_all_read.done", () => {
      setMarkAllReadProgress(null);
    });
    source.addEventListener("reclassify.done", (evt) => {
      try {
        const data = JSON.parse((evt as MessageEvent).data) as {
          mailbox_id?: number;
          state?: string;
          message?: string;
          updated?: number;
          processed?: number;
          failed?: number;
          total?: number;
        };
        const mailboxId = data.mailbox_id;
        if (mailboxId != null) {
          setReclassifyProgress((prev) => ({ ...prev, [mailboxId]: {
            processed: data.processed ?? data.total ?? 0, total: data.total ?? 0, failed: data.failed ?? 0,
          }}));
          const next = new Set(reclassifyingIdsRef.current);
          next.delete(mailboxId);
          reclassifyingIdsRef.current = next;
          setReclassifyingIds(next);
        }
        void refreshMailboxes();
        void loadFirstPage();
        setMailboxAction(
          mailboxId,
          data.state === "error" ? "error" : "success",
          data.message ||
            (data.state === "error"
              ? "Reclassify failed"
              : data.state === "stopped"
                ? "Reclassify stopped"
                : `Reclassify complete${data.updated != null ? ` — ${data.updated} updated` : ""}`),
        );
      } catch {
        void load();
      }
    });
    source.addEventListener("sync.done", (evt) => {
      try {
        const data = JSON.parse((evt as MessageEvent).data) as {
          mailbox_id?: number;
          state?: string;
          message?: string;
          imported?: number;
        };
        const mailboxId = data.mailbox_id;
        if (mailboxId != null) {
          const next = new Set(syncingIdsRef.current);
          next.delete(mailboxId);
          syncingIdsRef.current = next;
          setSyncingIds(next);
        }
        void refreshMailboxes();
        setMailboxAction(
          mailboxId,
          data.state === "error" ? "error" : "success",
          data.message ||
            (data.state === "error"
              ? "Sync failed"
              : data.state === "stopped"
                ? "Sync stopped"
                : `Sync complete${data.imported != null ? ` — ${data.imported} new` : ""}`),
        );
      } catch {
        void load();
      }
    });
    return () => source.close();
  }, [load, loadFirstPage, refreshMailboxes]);

  useEffect(() => {
    applyTheme(themePref);
    if (themePref !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => applyTheme("system");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [themePref]);

  useEffect(() => {
    applyFontFamily(fontFamily);
  }, [fontFamily]);

  useEffect(() => {
    applyFontSize(fontSize);
  }, [fontSize]);

  function persistPaneWidths() {
    try {
      localStorage.setItem(ACCOUNTS_W_KEY, String(Math.round(paneSizesRef.current.accounts)));
      localStorage.setItem(LIST_W_KEY, String(Math.round(paneSizesRef.current.list)));
      localStorage.setItem(LIST_H_KEY, String(Math.round(paneSizesRef.current.listHeight)));
    } catch {
      /* ignore */
    }
  }

  function onSplitterPointerDown(which: "accounts" | "list") {
    return (event: PointerEvent<HTMLButtonElement>) => {
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
      setResizing(which);
      dragRef.current = {
        which,
        axis: which === "list" && readingPane === "below" ? "y" : "x",
        startX: event.clientX,
        startY: event.clientY,
        startAccounts: paneSizesRef.current.accounts,
        startList: paneSizesRef.current.list,
        startListHeight: paneSizesRef.current.listHeight,
      };
    };
  }

  function onSplitterPointerMove(event: PointerEvent<HTMLButtonElement>) {
    const drag = dragRef.current;
    const shell = shellRef.current;
    if (!drag || !shell) return;
    const bounds = shell.getBoundingClientRect();
    if (drag.axis === "y") {
      const max = Math.max(LIST_HEIGHT_MIN, bounds.height - READER_HEIGHT_MIN - 6);
      const next = drag.startListHeight + event.clientY - drag.startY;
      setListH(Math.round(Math.min(max, Math.max(LIST_HEIGHT_MIN, next))));
      return;
    }
    const dx = event.clientX - drag.startX;
    if (drag.which === "accounts") {
      const reserved = readingPane === "right" ? drag.startList + READER_MIN + 12 : LIST_MIN + 6;
      const max = Math.max(ACCOUNTS_MIN, bounds.width - reserved);
      setAccountsW(Math.round(Math.min(max, Math.max(ACCOUNTS_MIN, drag.startAccounts + dx))));
    } else {
      const max = Math.max(LIST_MIN, bounds.width - drag.startAccounts - READER_MIN - 12);
      setListW(Math.round(Math.min(max, Math.max(LIST_MIN, drag.startList + dx))));
    }
  }

  function onSplitterPointerUp() {
    if (!dragRef.current) return;
    dragRef.current = null;
    setResizing(null);
    persistPaneWidths();
  }

  function onSplitterKeyDown(which: "accounts" | "list") {
    return (event: KeyboardEvent<HTMLButtonElement>) => {
      const shell = shellRef.current;
      if (!shell) return;
      const verticalStack = which === "list" && readingPane === "below";
      const decrementKey = verticalStack ? "ArrowUp" : "ArrowLeft";
      const incrementKey = verticalStack ? "ArrowDown" : "ArrowRight";
      if (event.key !== decrementKey && event.key !== incrementKey) return;
      event.preventDefault();
      const delta = (event.shiftKey ? 40 : 10) * (event.key === incrementKey ? 1 : -1);
      const bounds = shell.getBoundingClientRect();

      if (which === "accounts") {
        const reserved = readingPane === "right" ? paneSizesRef.current.list + READER_MIN + 12 : LIST_MIN + 6;
        const max = Math.max(ACCOUNTS_MIN, bounds.width - reserved);
        const next = Math.round(Math.min(max, Math.max(ACCOUNTS_MIN, paneSizesRef.current.accounts + delta)));
        setAccountsW(next);
        try { localStorage.setItem(ACCOUNTS_W_KEY, String(next)); } catch { /* ignore */ }
      } else if (verticalStack) {
        const max = Math.max(LIST_HEIGHT_MIN, bounds.height - READER_HEIGHT_MIN - 6);
        const next = Math.round(Math.min(max, Math.max(LIST_HEIGHT_MIN, paneSizesRef.current.listHeight + delta)));
        setListH(next);
        try { localStorage.setItem(LIST_H_KEY, String(next)); } catch { /* ignore */ }
      } else {
        const max = Math.max(LIST_MIN, bounds.width - paneSizesRef.current.accounts - READER_MIN - 12);
        const next = Math.round(Math.min(max, Math.max(LIST_MIN, paneSizesRef.current.list + delta)));
        setListW(next);
        try { localStorage.setItem(LIST_W_KEY, String(next)); } catch { /* ignore */ }
      }
    };
  }

  function chooseTheme(next: ThemePref) {
    setThemePref(next);
    saveThemePref(next);
  }

  function chooseViewMode(next: ViewMode) {
    setTableDialogOpen(false);
    setViewMode(next);
    saveViewMode(next);
  }

  function chooseInboxType(next: InboxType) {
    setInboxType(next);
    saveInboxType(next);
    filterRef.current = { ...filterRef.current, inboxType: next };
    void loadFirstPage();
  }

  function chooseConversationView(next: boolean) {
    setConversationView(next);
    saveConversationView(next);
    filterRef.current = { ...filterRef.current, conversationView: next };
    setThreadMessages([]);
    setOpenThreadIds([]);
    void loadFirstPage();
  }

  function chooseFontFamily(next: FontFamily) {
    setFontFamily(next);
    saveFontFamily(next);
  }

  function chooseFontSize(next: FontSize) {
    setFontSize(next);
    saveFontSize(next);
  }

  function openSettings(section: SettingsSection = "appearance") {
    const adminOnly = section === "ai" || section === "training";
    setSettingsSection(adminOnly && !sessionUser.is_admin ? "appearance" : section);
    setSettingsOpen(true);
  }

  function openMonitoring() {
    if (!sessionUser.is_admin) return;
    setSettingsOpen(false);
    setActivePage("monitoring");
  }

  function openStatistics() {
    if (!sessionUser.is_admin) return;
    setSettingsOpen(false);
    setActivePage("statistics");
  }

  const mailboxById = useMemo(() => {
    const map = new Map<number, Mailbox>();
    for (const box of mailboxes) map.set(box.id, box);
    return map;
  }, [mailboxes]);

  const orderedMailboxes = useMemo(() => {
    const index = new Map(accountOrder.map((id, position) => [id, position]));
    return [...mailboxes].sort((a, b) => (index.get(a.id) ?? Number.MAX_SAFE_INTEGER) - (index.get(b.id) ?? Number.MAX_SAFE_INTEGER));
  }, [mailboxes, accountOrder]);

  function onAccountDragStart(event: DragEvent<HTMLDivElement>, id: number) {
    setDraggedAccountId(id);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(id));
  }

  function onAccountDrop(event: DragEvent<HTMLDivElement>, targetId: number) {
    event.preventDefault();
    const sourceId = draggedAccountId ?? Number(event.dataTransfer.getData("text/plain"));
    setDraggedAccountId(null);
    if (!Number.isFinite(sourceId) || sourceId === targetId) return;
    setAccountOrder((previous) => {
      const ids = orderedMailboxes.map((mailbox) => mailbox.id);
      const source = ids.indexOf(sourceId);
      const target = ids.indexOf(targetId);
      if (source < 0 || target < 0) return previous;
      ids.splice(source, 1);
      ids.splice(target, 0, sourceId);
      try { localStorage.setItem(ACCOUNTS_ORDER_KEY, JSON.stringify(ids)); } catch { /* ignore */ }
      return ids;
    });
  }

  const inboxCount = useMemo(
    () => Object.values(mailboxUnreadCounts).reduce((sum, n) => sum + n, 0),
    [mailboxUnreadCounts]
  );

  const labelTallies = useMemo(() => {
    const tallies = {} as Record<EmailLabel, CountTally>;
    for (const key of CLASSIFY_LABELS) {
      tallies[key] = countCells.length
        ? tallyCounts(countCells, (cell) => cell.label === key && (selectedMailboxId == null || cell.mailbox_id === selectedMailboxId))
        : { total: labelCounts[key] ?? 0, unread: 0 };
    }
    return tallies;
  }, [countCells, labelCounts, selectedMailboxId]);

  const allCategoriesTally = useMemo(
    () => CLASSIFY_LABELS.reduce<CountTally>(
      (sum, key) => ({ total: sum.total + labelTallies[key].total, unread: sum.unread + labelTallies[key].unread }),
      { total: 0, unread: 0 },
    ),
    [labelTallies]
  );

  const renderCountBadge = (tally: CountTally, className: string) => {
    if (tally.unread <= 0) return null;
    return <span className={`${className} is-unread`} aria-label={`${tally.unread} unread`}>+{tally.unread}</span>;
  };

  const dateGroups = useMemo(() => groupEmailsByDate(emails), [emails]);

  const listRows = useMemo((): ListRow[] => {
    const rows: ListRow[] = [];
    const appendDateGroups = (prefix: string, items: EmailItem[]) => {
      for (const group of groupEmailsByDate(items)) {
        const key = `${prefix}:${group.key}`;
        rows.push({
          kind: "group",
          key,
          heading: group.heading,
          count: group.items.length,
        });
        if (collapsedDates.has(key)) continue;
        for (const email of group.items) {
          rows.push({ kind: "email", email });
        }
      }
    };

    if (inboxType === "unread_first") {
      const keptId = stickyUnreadId != null && stickyUnreadId === selectedId ? stickyUnreadId : null;
      const inUnread = (email: EmailItem) => !email.is_read || email.id === keptId;
      const unread = emails.filter(inUnread);
      if (keptId != null) {
        unread.sort((a, b) => receivedTime(b) - receivedTime(a) || b.id - a.id);
      }
      const read = emails.filter((email) => !inUnread(email));
      if (unread.length) {
        rows.push({
          kind: "group",
          key: "phase-unread",
          heading: "Unread",
          count: unread.filter((email) => !email.is_read).length,
        });
        if (!collapsedDates.has("phase-unread")) {
          appendDateGroups("unread", unread);
        }
      }
      if (read.length) {
        rows.push({
          kind: "group",
          key: "phase-read",
          heading: "Read",
          count: read.length,
        });
        if (!collapsedDates.has("phase-read")) {
          appendDateGroups("read", read);
        }
      }
      return rows;
    }

    for (const group of dateGroups) {
      rows.push({
        kind: "group",
        key: group.key,
        heading: group.heading,
        count: group.items.length,
      });
      if (collapsedDates.has(group.key)) continue;
      for (const email of group.items) {
        rows.push({ kind: "email", email });
      }
    }
    return rows;
  }, [collapsedDates, dateGroups, emails, inboxType, stickyUnreadId, selectedId]);

  const listRowsRef = useRef(listRows);
  listRowsRef.current = listRows;
  const viewModeRef = useRef(viewMode);
  viewModeRef.current = viewMode;
  const readingPaneRef = useRef(readingPane);
  readingPaneRef.current = readingPane;
  const selectedIdRef = useRef(selectedId);
  selectedIdRef.current = selectedId;
  const openEmailRef = useRef<(id: number) => void>(() => {});
  const selectTableEmailRef = useRef<(id: number) => void>(() => {});
  const openTableEmailRef = useRef<(id: number) => void>(() => {});
  const prefetchTimerRef = useRef<number | null>(null);
  const threadBodyRequests = useRef(new Set<number>());

  const listVirtualizer = useVirtualizer({
    count: loading ? 0 : listRows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: (index) =>
      listRowsRef.current[index]?.kind === "group"
        ? LIST_ROW_GROUP_H
        : emailRowHeight(viewModeRef.current),
    overscan: LIST_OVERSCAN,
    getItemKey: (index) => {
      const row = listRowsRef.current[index];
      if (!row) return index;
      return row.kind === "group" ? `g-${row.key}` : `e-${row.email.id}`;
    },
  });

  const openMessage = useCallback((id: number) => {
    if (readingPaneRef.current === "no_split") {
      if (viewModeRef.current === "table") selectTableEmailRef.current(id);
      else openTableEmailRef.current(id);
    } else {
      void openEmailRef.current(id);
    }
  }, []);

  const openMessageDirect = useCallback((id: number) => {
    if (readingPaneRef.current === "no_split") openTableEmailRef.current(id);
    else void openEmailRef.current(id);
  }, []);

  const prefetchMessage = useCallback((id: number) => {
    if (prefetchTimerRef.current != null) window.clearTimeout(prefetchTimerRef.current);
    prefetchTimerRef.current = window.setTimeout(() => {
      prefetchTimerRef.current = null;
      if (selectedIdRef.current === id) return;
      prefetchEmailDetail(id);
    }, 90);
  }, []);

  const virtualItems = listVirtualizer.getVirtualItems();
  const lastVirtualIndex = virtualItems.length ? virtualItems[virtualItems.length - 1].index : -1;

  useEffect(() => {
    listVirtualizer.measure();
  }, [fontFamily, fontSize, viewMode, listVirtualizer]);

  useEffect(() => {
    if (loading || lastVirtualIndex < 0) return;
    if (lastVirtualIndex >= listRows.length - LIST_OVERSCAN) void loadMore();
  }, [lastVirtualIndex, listRows.length, loadMore, loading]);

  function toggleDateGroup(key: string) {
    setCollapsedDates((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function closeAddAccount() {
    setAddAccountOpen(false);
    setDialogStep("type");
    setConnectEmail("");
  }

  function chooseAccountType(provider: Provider) {
    setConnectProvider(provider);
    setDialogStep("email");
  }

  function guessProviderFromEmail(email: string): Provider | null {
    const domain = email.trim().toLowerCase().split("@")[1] ?? "";
    if (["gmail.com", "googlemail.com"].includes(domain)) return "google";
    if (["outlook.com", "hotmail.com", "live.com", "msn.com", "office365.com"].includes(domain)) {
      return "microsoft";
    }
    return null;
  }

  function connectWithEmail() {
    const email = connectEmail.trim();
    if (!email || !email.includes("@")) {
      setBanner("Enter a valid email address.");
      return;
    }
    const provider = guessProviderFromEmail(email) ?? connectProvider;
    window.location.href = oauthStartUrl(provider, { email });
  }

  function onReconnect(id: number) {
    const mailbox = mailboxById.get(id);
    if (!mailbox) return;
    window.location.href = oauthStartUrl(mailbox.provider, {
      email: mailbox.email_address,
      name: mailbox.display_name ?? undefined,
    });
  }

  function onConnectEmailSubmit(event: FormEvent) {
    event.preventDefault();
    connectWithEmail();
  }

  async function onDisconnect(id: number) {
    await disconnectMailbox(id);
    setMailboxActions((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
    if (selected?.mailbox_id === id) {
      setSelected(null);
      setSelectedId(null);
    }
    if (selectedMailboxId === id) setSelectedMailboxId(null);
    await load();
  }

  function onAccountColor(id: number, calendarColor: string) {
    const previous = mailboxes.find((box) => box.id === id)?.calendar_color ?? null;
    setMailboxes((current) => current.map((box) => box.id === id ? { ...box, calendar_color: calendarColor } : box));
    void updateMailboxColor(id, calendarColor).catch(() => {
      setMailboxes((current) => current.map((box) => box.id === id ? { ...box, calendar_color: previous } : box));
    });
  }

  async function onSync(id: number, selectAccount = true) {
    if (selectAccount) selectMailbox(id);
    setIdInSet(setSyncingIds, id, true);
    syncingIdsRef.current = new Set(syncingIdsRef.current).add(id);
    setMailboxAction(id, "sync", "Starting full rescan...");
    try {
      const started = await syncMailbox(id);
      if (syncingIdsRef.current.has(id)) {
        setMailboxAction(id, "sync", started.message || "Syncing in the background…");
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Sync failed";
      setMailboxAction(id, "error", msg);
      setIdInSet(setSyncingIds, id, false);
      const next = new Set(syncingIdsRef.current);
      next.delete(id);
      syncingIdsRef.current = next;
    }
  }

  async function onSyncAllEmails() {
    setSyncMenuOpen(false);
    const activeMailboxes = mailboxesRef.current.filter((mailbox) => mailbox.is_active);
    await Promise.all(activeMailboxes.map((mailbox) => onSync(mailbox.id, false)));
  }

  function openCalendar() {
    setCalendarMounted(true);
    setActivePage("calendar");
  }

  function onSyncCalendar() {
    setSyncMenuOpen(false);
    openCalendar();
    setCalendarRefreshKey((current) => current + 1);
  }

  async function onStopSync(id: number, selectAccount = true) {
    if (selectAccount) selectMailbox(id);
    try {
      const stopped = await stopSyncMailbox(id);
      setMailboxAction(id, "sync", stopped.message || "Stopping sync…");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to stop sync";
      setMailboxAction(id, "error", msg);
    }
  }

  async function onReclassify(id: number, selectAccount = true) {
    if (selectAccount) selectMailbox(id);
    setIdInSet(setReclassifyingIds, id, true);
    reclassifyingIdsRef.current = new Set(reclassifyingIdsRef.current).add(id);
    setMailboxAction(id, "reclassify", "Reclassifying… running AI labels");
    try {
      const started = await reclassifyMailbox(id);
      if (reclassifyingIdsRef.current.has(id)) {
        setMailboxAction(id, "reclassify", started.message || "Reclassifying in the background…");
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Reclassify failed";
      setMailboxAction(id, "error", msg);
      setIdInSet(setReclassifyingIds, id, false);
      const next = new Set(reclassifyingIdsRef.current);
      next.delete(id);
      reclassifyingIdsRef.current = next;
    }
  }

  async function onStopReclassify(id: number, selectAccount = true) {
    if (selectAccount) selectMailbox(id);
    try {
      const stopped = await stopReclassifyMailbox(id);
      setMailboxAction(id, "reclassify", stopped.message || "Stopping reclassify…");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to stop reclassify";
      setMailboxAction(id, "error", msg);
    }
  }

  function onChangeCategory(nextLabel: EmailLabel) {
    if (!selected || savingLabel || pendingLabel || selected.label === nextLabel) return;
    setPendingSubtype("");
    setPendingLabel({ emailId: selected.id, nextLabel });
  }

  function onPickCategory(choice: CategoryChoice) {
    setCategoryPickerOpen(false);
    if (savingLabel || pendingLabel) return;
    const targetId = selected?.id ?? selectedId;
    if (targetId == null) return;
    const listItem = emails.find((e) => e.id === targetId);
    const currentLabel = selected?.label ?? listItem?.label;
    const currentSubtype = selected?.interview_subtype ?? listItem?.interview_subtype ?? null;
    if (currentLabel == null) return;
    const sameSubtype = choice.label !== "interview_scheduled" || choice.subtype === currentSubtype;
    if (currentLabel === choice.label && sameSubtype) return;
    setPendingSubtype(choice.subtype ?? "");
    setPendingLabel({ emailId: targetId, nextLabel: choice.label });
  }

  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key.toLowerCase() !== "l" || event.ctrlKey || event.metaKey || event.altKey || event.repeat) return;
      if (activePage !== "inbox" || settingsOpen || categoryPickerOpen || selectedId == null || savingLabel || pendingLabel) return;
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (target?.closest("input, textarea, select, [contenteditable='true'], [contenteditable='']")) return;
      if (target instanceof HTMLIFrameElement && target.classList.contains("email-body-frame")) return;
      if (document.querySelector(".dialog-backdrop, .category-picker-backdrop, .mail-alert-backdrop")) return;
      event.preventDefault();
      setCategoryPickerOpen(true);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [activePage, settingsOpen, categoryPickerOpen, selectedId, savingLabel, pendingLabel]);

  async function applyCategoryChange(useTraining: boolean) {
    if (!pendingLabel || savingLabel) return;
    if (pendingLabel.nextLabel === "interview_scheduled" && !pendingSubtype) return;
    const emailId = pendingLabel.emailId;
    const nextLabel = pendingLabel.nextLabel;
    const subtype = nextLabel === "interview_scheduled" ? pendingSubtype as InterviewSubtype : undefined;
    const baseItem = (selected?.id === emailId ? selected : null) ?? emails.find((e) => e.id === emailId);
    if (!baseItem) return;
    const previous = baseItem.label;
    const previousSubtype = baseItem.interview_subtype;
    setPendingLabel(null);
    setSaveTraining(useTraining);
    setSavingLabel(true);
    setSelected((prev) => (prev && prev.id === emailId ? { ...prev, label: nextLabel, interview_subtype: subtype ?? null } : prev));
    setEmails((prev) => {
      const currentLabel = filterRef.current.label;
      const currentSubtype = filterRef.current.interviewSubtype;
      if ((currentLabel !== "all" && currentLabel !== nextLabel)
        || (currentSubtype !== "all" && currentSubtype !== subtype)) {
        return prev.filter((item) => item.id !== emailId);
      }
      return prev.map((item) => (item.id === emailId ? { ...item, label: nextLabel, interview_subtype: subtype ?? null } : item));
    });
    try {
      const updated = await updateEmailLabel(emailId, nextLabel, useTraining, subtype);
      setSelected((prev) => (prev && prev.id === emailId ? { ...prev, ...updated } : prev));
      setEmails((prev) => prev.map((item) => item.id === emailId ? { ...item, ...updated } : item));
      if (useTraining) {
        setUnusedTraining((count) => count + 1);
        void refreshPromptStatus();
      }
    } catch (err) {
      setSelected((prev) => (prev && prev.id === emailId ? { ...prev, label: previous, interview_subtype: previousSubtype } : prev));
      setEmails((prev) =>
        prev.map((item) => (item.id === emailId ? { ...item, label: previous } : item))
      );
      setBanner(err instanceof Error ? err.message : "Could not update category");
    } finally {
      setSavingLabel(false);
    }
  }

  async function onUpdatePrompt() {
    setUpdatingPrompt(true);
    setBanner("Updating classify prompt from training data…");
    try {
      const result = await updateClassifyPrompt();
      await refreshPromptStatus();
      setBanner(result.message || `Classify prompt updated from ${result.example_count} example(s)`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to update classify prompt";
      setBanner(msg);
    } finally {
      setUpdatingPrompt(false);
    }
  }

  async function onOpenEmail(id: number) {
    setActivePage("inbox");
    const request = ++detailGen.current;
    const target = emails.find((item) => item.id === id);
    const shouldMarkRead = Boolean(target && !target.is_read);
    const cached = peekEmailDetail(id) ?? null;
    setStickyUnreadId((current) => (current === id ? current : target && !target.is_read ? id : null));
    setSelectedId(id);
    setSelected(cached ?? (target ? { ...target, body_text: "", body_html: "" } : null));
    setThreadMessages([]);
    setOpenThreadIds([]);
    setSaveTraining(false);
    setPendingLabel(null);
    setCategoryPickerOpen(false);
    const cachedBody = Boolean(cached && (cached.body_html || cached.body_text));
    setLoadingDetail(!cachedBody);
    try {
      const pending = cached ? undefined : peekEmailDetailRequest(id);
      let detail = cached ?? (pending ? await pending : null);
      if (request !== detailGen.current) return;
      if (detail) {
        setSelected(detail);
        setLoadingDetail(false);
        // In conversation view the representative message can already be read
        // while an older message in its thread is still unread.
        if (shouldMarkRead || !detail.is_read) {
          detail = await fetchEmailDetail(id, true, conversationView);
          if (request !== detailGen.current) return;
          setSelected(detail);
        }
      } else {
        detail = await fetchEmailDetail(id, true, conversationView);
        if (request !== detailGen.current) return;
        setSelected(detail);
      }
      // Keep state updaters pure. React Strict Mode may invoke an updater more
      // than once in development; nesting the badge update inside setEmails
      // made a single opened unread message subtract two from its badge.
      const opened = emails.find((item) => item.id === id);
      if (opened && !opened.is_read) {
        setMailboxUnreadCounts((counts) => ({
          ...counts,
          [opened.mailbox_id]: Math.max(0, (counts[opened.mailbox_id] ?? 1) - 1),
        }));
        const cell = { mailbox_id: opened.mailbox_id, label: opened.label, folder: mailFolderOf(opened) };
        setCountCells((prev) => adjustCountCell(adjustCountCell(prev, { ...cell, unread: true }, -1), { ...cell, unread: false }, 1));
      }
      setEmails((prev) => {
        return sortEmailsForInbox(
          prev.map((item) =>
            item.id === id
              ? { ...item, is_read: true, folder: detail.folder ?? item.folder }
              : item
          ),
          filterRef.current.inboxType,
        );
      });
    } catch (err) {
      if (request === detailGen.current) {
        setBanner(err instanceof Error ? err.message : "Could not open email");
        if (readingPane === "no_split") setTableDialogOpen(false);
      }
    } finally {
      if (request === detailGen.current) setLoadingDetail(false);
    }
  }

  function selectTableEmail(id: number) {
    if (id === selectedId) return;
    ++detailGen.current;
    setSelectedId(id);
    setSelected(null);
    setLoadingDetail(false);
    prefetchEmailDetail(id);
  }

  function openTableEmail(id: number) {
    setTableDialogOpen(true);
    void onOpenEmail(id);
  }

  openEmailRef.current = onOpenEmail;
  selectTableEmailRef.current = selectTableEmail;
  openTableEmailRef.current = openTableEmail;

  useEffect(() => {
    if (!conversationView || selectedId == null) {
      setThreadMessages([]);
      return;
    }
    let cancel = false;
    threadBodyRequests.current.clear();
    fetchEmailConversation(selectedId)
      .then((items) => {
        if (cancel) return;
        setThreadMessages(items.map((item) => {
          const cached = peekEmailDetail(item.id);
          return { ...item, body_text: cached?.body_text ?? "", body_html: cached?.body_html ?? "" };
        }));
        const newestId = items[items.length - 1]?.id;
        setOpenThreadIds(
          items
            .filter((message) => message.id === newestId || message.id === selectedId || !message.is_read)
            .map((message) => message.id),
        );
      })
      .catch(() => {
        if (!cancel) {
          setThreadMessages([]);
          setOpenThreadIds([]);
        }
      });
    return () => {
      cancel = true;
    };
  }, [conversationView, selectedId]);

  useEffect(() => {
    if (!conversationView || selectedId == null) return;
    const missing = openThreadIds.filter((id) => {
      if (threadBodyRequests.current.has(id)) return false;
      const message = threadMessages.find((item) => item.id === id);
      if (!message || message.body_html || message.body_text) return false;
      if (id === selectedId && selected && (selected.body_html || selected.body_text)) return false;
      return true;
    });
    for (const id of missing) {
      threadBodyRequests.current.add(id);
      void fetchEmailDetail(id, false).then((detail) => {
        setThreadMessages((prev) => prev.map((item) => item.id === id ? { ...item, ...detail } : item));
      }).catch(() => {
        threadBodyRequests.current.delete(id);
      });
    }
  }, [conversationView, openThreadIds, selected, selectedId, threadMessages]);

  function closeTableDialog() {
    ++detailGen.current;
    setTableDialogOpen(false);
    setLoadingDetail(false);
  }

  useEffect(() => {
    if (!tableDialogOpen) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    readerCloseRef.current?.focus();
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (document.querySelector(".dialog-backdrop")) return;
      if (event.key === "Escape") {
        event.preventDefault();
        closeTableDialog();
      } else if (event.key === "Tab") {
        const focusable = Array.from(readerDialogRef.current?.querySelectorAll<HTMLElement>(
          "button:not(:disabled), select:not(:disabled), input:not(:disabled), a[href], [tabindex]:not([tabindex='-1'])",
        ) ?? []);
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (!readerDialogRef.current?.contains(document.activeElement)) {
          event.preventDefault();
          first.focus();
        } else if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previousFocus?.focus();
    };
  }, [tableDialogOpen]);

  useEffect(() => {
    if (!syncMenuOpen) return;
    const closeOnPointerDown = (event: globalThis.PointerEvent) => {
      if (!syncMenuRef.current?.contains(event.target as Node)) setSyncMenuOpen(false);
    };
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") setSyncMenuOpen(false);
    };
    document.addEventListener("pointerdown", closeOnPointerDown);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnPointerDown);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [syncMenuOpen]);

  function onMessageListKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (viewMode === "table" && readingPane === "no_split" && event.key === "Enter" && event.target === event.currentTarget && selectedId != null) {
      event.preventDefault();
      openTableEmail(selectedId);
      return;
    }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const target = event.target as HTMLElement;
    if (target.closest("input, textarea, select, [contenteditable='true']")) return;
    if (moveListSelection(event.key === "ArrowDown" ? 1 : -1)) event.preventDefault();
  }

  function moveListSelection(step: 1 | -1) {
    const emailRows = listRows
      .map((row, index) => row.kind === "email" ? { id: row.email.id, index } : null)
      .filter((row): row is { id: number; index: number } => row !== null);
    if (!emailRows.length) return false;

    const current = emailRows.findIndex((row) => row.id === selectedId);
    const next = current < 0
      ? step === 1 ? 0 : emailRows.length - 1
      : Math.max(0, Math.min(emailRows.length - 1, current + step));
    const row = emailRows[next];
    scrollRef.current?.focus({ preventScroll: true });
    if (row.id === selectedId) return true;
    listVirtualizer.scrollToIndex(row.index, { align: "auto" });
    if (viewMode === "table" && readingPane === "no_split") selectTableEmail(row.id);
    else void onOpenEmail(row.id);
    return true;
  }

  const moveListSelectionRef = useRef(moveListSelection);
  moveListSelectionRef.current = moveListSelection;

  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
      if (activePage !== "inbox" || settingsOpen) return;
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (target && target !== document.body) {
        if (target.closest("input, textarea, select, [contenteditable='true'], [contenteditable=''], [role='listbox'], [role='menu'], [role='slider'], .reader-article, .pane-splitter, .message-scroll")) return;
      }
      if (document.querySelector(".dialog-backdrop, .category-picker-backdrop, .mail-alert-backdrop")) return;
      if (moveListSelectionRef.current(event.key === "ArrowDown" ? 1 : -1)) event.preventDefault();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [activePage, settingsOpen]);

  function selectMailbox(id: number | null) {
    setActivePage("inbox");
    scrollRef.current?.scrollTo(0, 0);
    if (selectedMailboxId === id) {
      setEmails([]);
      void loadFirstPage();
      return;
    }

    // State updates are asynchronous. Keep the request's filter snapshot in
    // sync with the clicked row before loading, rather than waiting for the
    // effect after render. This prevents the previous mailbox's date headings
    // and counts from remaining visible during navigation.
    filterRef.current = { ...filterRef.current, mailboxId: id };
    skipNextMailboxFilterLoad.current = true;
    setSelectedMailboxId(id);
    setEmails([]);
    void loadFirstPage();
  }

  async function filePayload(files: File[]) {
    return Promise.all(files.map((file) => new Promise<{ name: string; content_type: string; content_base64: string }>((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error(`Could not read ${file.name}`));
      reader.onload = () => resolve({ name: file.name, content_type: file.type || "application/octet-stream", content_base64: String(reader.result).split(",")[1] || "" });
      reader.readAsDataURL(file);
    })));
  }

  async function sendReply() {
    if (!selected || !replyMailbox || !replyText.trim()) return;
    const run = async () => {
      setSendingReply(true);
      try {
        await sendEmail({ mailbox_id: replyMailbox.id, to_address: selected.sender.match(/<([^>]+)>/)?.[1] ?? selected.sender, subject: /^re:/i.test(selected.subject) ? selected.subject : `Re: ${selected.subject}`, body_text: replyText, attachments: await filePayload(replyFiles), reply_to_email_id: selected.id });
        setBanner(scheduleAt ? "Reply scheduled and sent." : "Reply sent. It is now in Sent.");
        setReplyOpen(false); setReplyText(""); setReplyInstruction(""); setReplyFiles([]); setScheduleAt("");
        if (folder === "sent") void loadFirstPage();
      } catch (err) { setBanner(err instanceof Error ? err.message : "Could not send reply"); }
      finally { setSendingReply(false); }
    };
    const when = scheduleAt ? new Date(scheduleAt).getTime() : 0;
    if (when > Date.now()) { window.setTimeout(() => void run(), when - Date.now()); setBanner(`Reply scheduled for ${new Date(when).toLocaleString()}. Keep this app open until it sends.`); return; }
    await run();
  }

  async function draftAiReply() {
    if (!selected) return;
    setAiDrafting(true);
    try { setReplyText((await createAiReply(selected.id, replyInstruction.trim())).body_text); }
    catch (err) { setBanner(err instanceof Error ? err.message : "Could not create AI draft"); }
    finally { setAiDrafting(false); }
  }

  function chooseSchedulePreset(daysFromToday: number, hour: number) {
    const date = new Date();
    date.setDate(date.getDate() + daysFromToday);
    date.setHours(hour, 0, 0, 0);
    setScheduleAt(`${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}T${String(hour).padStart(2, "0")}:00`);
  }

  function selectLabel(next: "all" | EmailLabel) {
    scrollRef.current?.scrollTo(0, 0);
    if (interviewSubtype !== "all") {
      setInterviewSubtype("all");
      if (label === next) return;
    }
    if (label === next) {
      setEmails([]);
      void loadFirstPage();
      return;
    }
    setLabel(next);
  }

  function selectInterviewSubtype(next: "all" | InterviewSubtype) {
    if (interviewSubtype === next) return;
    scrollRef.current?.scrollTo(0, 0);
    setInterviewSubtype(next);
  }

  function selectMailboxFolder(id: number | null, nextFolder: "all" | MailFolder) {
    setActivePage("inbox");
    scrollRef.current?.scrollTo(0, 0);
    if (selectedMailboxId === id && folder === nextFolder) {
      setEmails([]);
      void loadFirstPage();
      return;
    }
    filterRef.current = { ...filterRef.current, mailboxId: id, folder: nextFolder };
    skipNextMailboxFilterLoad.current = true;
    setSelectedMailboxId(id);
    setFolder(nextFolder);
    setEmails([]);
    void loadFirstPage();
  }

  function chooseReadingPane(next: ReadingPane) {
    setTableDialogOpen(false);
    setReadingPane(next);
    saveReadingPane(next);
  }

  function toggleSidebarSection(section: keyof typeof sidebarSections) {
    setSidebarSections((current) => ({ ...current, [section]: !current[section] }));
  }

  const selectedMailbox = selectedMailboxId != null ? mailboxById.get(selectedMailboxId) : null;
  const replyMailbox = selected ? mailboxById.get(selected.mailbox_id) ?? selectedMailbox : selectedMailbox;
  const scopeUnread = useMemo(() => {
    if (selectedMailboxId == null) {
      return Object.values(mailboxUnreadCounts).reduce((sum, n) => sum + n, 0);
    }
    return mailboxUnreadCounts[selectedMailboxId] ?? 0;
  }, [selectedMailboxId, mailboxUnreadCounts]);

  const filteredScopeUnread = useMemo(
    () => tallyCounts(
      countCells,
      (cell) => (selectedMailboxId == null || cell.mailbox_id === selectedMailboxId)
        && (label === "all" || cell.label === label),
    ).unread,
    [countCells, label, selectedMailboxId],
  );

  useEffect(() => {
    if (inboxType !== "unread_first" || loading || loadingMore || !hasMore) return;
    if (searchQuery || interviewSubtype !== "all" || filteredScopeUnread <= 0) return;
    if (emails.some((email) => !email.is_read)) return;

    // Reading every unread item in the current page moves those rows into the
    // read bucket. They still occupy enough space that the bottom-of-list
    // pagination trigger cannot fire, even when older unread mail remains.
    // Pull the next cursor page so the unread badge and list cannot disagree.
    void loadMore();
  }, [
    emails,
    filteredScopeUnread,
    hasMore,
    inboxType,
    interviewSubtype,
    loadMore,
    loading,
    loadingMore,
    searchQuery,
  ]);

  async function confirmMarkAllRead() {
    const scopeId = selectedMailboxId;
    setMarkingAllRead(true);
    setMarkAllReadProgress({ marked: 0, total: scopeUnread });
    try {
      await markAllRead(scopeId);
      setEmails((prev) =>
        sortEmailsForInbox(
          prev.map((item) =>
            scopeId == null || item.mailbox_id === scopeId ? { ...item, is_read: true } : item
          ),
          filterRef.current.inboxType,
        )
      );
      setSelected((prev) =>
        prev && (scopeId == null || prev.mailbox_id === scopeId) ? { ...prev, is_read: true } : prev
      );
      setMailboxUnreadCounts((counts) => {
        if (scopeId == null) {
          const next = { ...counts };
          for (const key of Object.keys(next)) next[Number(key)] = 0;
          return next;
        }
        return { ...counts, [scopeId]: 0 };
      });
      setCountCells((prev) => {
        let next = prev;
        for (const cell of prev) {
          if (!cell.unread || cell.count <= 0 || (scopeId != null && cell.mailbox_id !== scopeId)) continue;
          next = adjustCountCell(adjustCountCell(next, cell, -cell.count), { ...cell, unread: false }, cell.count);
        }
        return next;
      });
      setMarkAllOpen(false);
    } catch (err) {
      setBanner(err instanceof Error ? err.message : "Could not mark messages read");
    } finally {
      setMarkingAllRead(false);
      setMarkAllReadProgress(null);
    }
  }
  const readerMailbox = selected != null ? mailboxById.get(selected.mailbox_id) : undefined;
  const readerAccount = readerMailbox?.email_address ?? "";
  const selectedSenderAddress = selected
    ? (selected.sender.match(/<([^>]+)>/)?.[1] ?? (selected.sender.includes("@") ? selected.sender.trim() : "")).trim()
    : "";
  const conversationMessages = conversationView && threadMessages.length > 1 ? threadMessages : [];
  const selectedThreadIndex = selected == null ? -1 : conversationMessages.findIndex((message) => message.id === selected.id);
  const showConversation = selectedThreadIndex >= 0;
  const earlierThread = showConversation ? conversationMessages.slice(0, selectedThreadIndex) : [];
  const laterThread = showConversation ? conversationMessages.slice(selectedThreadIndex + 1) : [];
  const selectedThreadOpen = !showConversation || (selected != null && openThreadIds.includes(selected.id));

  useEffect(() => {
    if (!showConversation) return;
    const frame = window.requestAnimationFrame(() => {
      const node = conversationScrollRef.current;
      const selectedCard = node?.querySelector<HTMLElement>(".thread-card.is-selected");
      if (!node || !selectedCard) return;
      const top = selectedCard.getBoundingClientRect().top - node.getBoundingClientRect().top + node.scrollTop;
      node.scrollTop = Math.max(0, top - 8);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [showConversation, selected?.id, threadMessages.length]);

  function renderThreadMessage(message: EmailDetail) {
    const open = openThreadIds.includes(message.id);
    const name = senderName(message.sender);
    const preview = (message.snippet || message.body_text || "").replace(/\s+/g, " ").trim();
    const read = message.id === selected?.id ? Boolean(selected.is_read) : message.is_read;
    return (
      <section key={message.id} className={`thread-card${open ? " is-open" : ""}${read ? "" : " is-unread"}`}>
        <button
          type="button"
          className="thread-summary"
          aria-expanded={open}
          onClick={() => setOpenThreadIds((ids) => open ? ids.filter((item) => item !== message.id) : [...ids, message.id])}
        >
          <span className={`avatar soft label-${message.label}`}>{initialsFrom(name)}</span>
          <span className="thread-summary-text">
            <strong>{name}</strong>
            {!open && preview ? <span className="thread-snippet">{preview}</span> : null}
          </span>
          <time>{formatReaderTime(message.received_at)}</time>
        </button>
        {open && (
          <>
            {readerAccount ? <p className="thread-to">to <span className="to-address">{readerAccount}</span></p> : null}
            {message.label === "application_action_required" ? (
              <ApplicationActionCopies
                subject={message.subject}
                html={message.body_html}
                text={message.body_text}
                snippet={message.snippet}
              />
            ) : null}
            <EmailBody html={message.body_html} text={message.body_text} snippet={message.snippet} />
          </>
        )}
      </section>
    );
  }
  const aiReady = Boolean(aiStatus?.ready) && !aiStatusOffline;
  const aiLabel = aiStatus?.provider === "ollama"
    ? aiReady ? "Local AI on" : "Local AI off"
    : aiStatus?.provider === "gemini"
      ? aiReady ? "Gemini on" : "Gemini not configured"
      : aiReady ? "OpenAI on" : "AI not configured";
  const aiTitle = aiStatus?.model ? `${aiLabel} · ${aiStatus.model}` : aiLabel;
  const emailSyncState = syncingIds.size > 0 ? "syncing" : autoSyncOffline ? "offline" : autoSyncStatus?.state ?? "loading";
  const completedSyncTimes = (autoSyncStatus?.mailboxes ?? [])
    .map((mailbox) => {
      const times = [mailbox.last_sync_completed_at, mailbox.webhook_verified ? mailbox.last_webhook_success_at : null]
        .map((value) => value ? Date.parse(value) : Number.NaN)
        .filter(Number.isFinite);
      return times.length ? Math.max(...times) : Number.NaN;
    })
    .filter(Number.isFinite);
  const oldestCompletedSync = completedSyncTimes.length === (autoSyncStatus?.connected_accounts ?? 0)
    && completedSyncTimes.length > 0
    ? Math.min(...completedSyncTimes)
    : null;
  const syncAgeLabel = oldestCompletedSync == null
    ? null
    : (() => {
        const seconds = Math.max(0, Math.floor((Date.now() - oldestCompletedSync) / 1000));
        if (seconds < 10) return "Checked just now";
        if (seconds < 60) return `Checked ${seconds}s ago`;
        const minutes = Math.floor(seconds / 60);
        if (minutes < 60) return `Checked ${minutes}m ago`;
        return `Checked ${Math.floor(minutes / 60)}h ago`;
      })();
  const connectedAccounts = autoSyncStatus?.connected_accounts ?? 0;
  const pollingAccounts = autoSyncStatus?.polling_accounts
    ?? connectedAccounts - (autoSyncStatus?.webhook_accounts ?? 0);
  const accountSyncDetails = Object.fromEntries(
    (autoSyncStatus?.mailboxes ?? []).map((mailbox) => [mailbox.mailbox_id, mailbox]),
  );

  const systemIssues: SystemIssue[] = [];
  const recheckStatus = async () => {
    setRecheckingStatus(true);
    try {
      await Promise.all([refreshAutoSyncStatus(), refreshAiStatus()]);
    } finally {
      setRecheckingStatus(false);
    }
  };
  if (autoSyncOffline && aiStatusOffline) {
    systemIssues.push({
      id: "service-offline",
      kind: "service",
      severity: "error",
      title: "The app's background service is not responding",
      detail: "Email sync and AI classification are both unavailable. Make sure the backend is running, then check again.",
    });
  } else {
    if (autoSyncOffline) {
      systemIssues.push({
        id: "sync-offline",
        kind: "sync",
        severity: "error",
        title: "Can't reach the email sync service",
        detail: "New mail isn't being downloaded. Make sure the backend is running, then check again.",
      });
    } else if (autoSyncStatus) {
      const syncMailboxes = autoSyncStatus.mailboxes ?? [];
      const expired = syncMailboxes.filter((mailbox) => mailbox.needs_reconnect);
      if (expired.length) {
        systemIssues.push({
          id: `sync-reconnect-${expired.map((mailbox) => mailbox.mailbox_id).join(",")}`,
          kind: "sync",
          severity: "error",
          title: expired.length === 1 ? "An account's sign-in expired" : `${expired.length} accounts' sign-ins expired`,
          detail: "These accounts stopped syncing. Reconnect each one to sign in again — your stored emails are kept.",
          items: expired.map((mailbox) => ({
            id: String(mailbox.mailbox_id),
            label: mailbox.email_address,
            note: mailbox.provider === "google" ? "Gmail" : "Outlook",
            action: { label: "Reconnect", onClick: () => onReconnect(mailbox.mailbox_id), primary: true },
          })),
        });
      }
      const expiredIds = new Set(expired.map((mailbox) => mailbox.mailbox_id));
      const failing = autoSyncStatus.problem_mailboxes.filter((problem) => !expiredIds.has(problem.mailbox_id));
      if (failing.length) {
        systemIssues.push({
          id: `sync-failed-${failing.map((problem) => problem.mailbox_id).join(",")}`,
          kind: "sync",
          severity: "error",
          title: failing.length === 1 ? "An account failed to sync" : `${failing.length} accounts failed to sync`,
          detail: "The app will keep retrying automatically. You can also try syncing now.",
          items: failing.map((problem) => ({
            id: String(problem.mailbox_id),
            label: problem.email_address,
            note: problem.message,
            action: { label: "Sync now", onClick: () => void onSync(problem.mailbox_id, false) },
          })),
        });
      }
      if (autoSyncStatus.state === "stopped" && autoSyncStatus.connected_accounts > 0) {
        systemIssues.push({
          id: "sync-stopped",
          kind: "sync",
          severity: "warning",
          title: "Automatic email sync is paused",
          detail: "The background sync hasn't run recently, so new mail may be missing. Restarting the backend usually fixes this.",
        });
      }
    }
    if (aiStatusOffline) {
      systemIssues.push({
        id: "ai-offline",
        kind: "ai",
        severity: "error",
        title: "Can't check the AI model",
        detail: "New emails won't be classified until the AI service responds.",
        actions: sessionUser.is_admin
          ? [{ label: "Open AI settings", onClick: () => openSettings("ai") }]
          : undefined,
      });
    } else if (aiStatus && !aiStatus.ready) {
      const providerName = aiStatus.provider === "ollama" ? "Local AI (Ollama)" : aiStatus.provider === "gemini" ? "Gemini" : "OpenAI";
      systemIssues.push({
        id: `ai-not-ready-${aiStatus.provider}-${aiStatus.model}`,
        kind: "ai",
        severity: "error",
        title: `${providerName} isn't ready`,
        detail: `${aiStatus.reason ?? `The model ${aiStatus.model} can't be used right now.`} New emails won't be classified until this is fixed.`,
        actions: sessionUser.is_admin
          ? [{ label: "Open AI settings", onClick: () => openSettings("ai"), primary: true }]
          : undefined,
      });
    }
  }
  const issueKey = systemIssues.map((issue) => issue.id).join("|");
  useEffect(() => {
    if (!issueKey) {
      setStableIssueKey("");
      return;
    }
    const timer = window.setTimeout(() => setStableIssueKey(issueKey), 5_000);
    return () => window.clearTimeout(timer);
  }, [issueKey]);
  const visibleIssues = stableIssueKey && stableIssueKey === issueKey ? systemIssues : [];
  const systemAlertOpen = visibleIssues.length > 0 && issueKey !== dismissedIssueKey;
  const dismissSystemAlert = useCallback(() => {
    setDismissedIssueKey(issueKey);
    sessionStorage.setItem("systemAlertDismissed", issueKey);
  }, [issueKey]);
  const reopenSystemAlert = () => {
    setDismissedIssueKey("");
    sessionStorage.removeItem("systemAlertDismissed");
  };
  const ngrokState = autoSyncStatus?.ngrok?.state ?? null;
  const ngrokStatusLabel = ngrokState === "online" ? "ngrok on" : ngrokState === "offline" ? "ngrok off" : ngrokState === "mismatch" ? "ngrok mismatch" : null;
  const deliveryMethod = !autoSyncStatus || autoSyncStatus.connected_accounts === 0
    ? null
    : pollingAccounts <= 0
      ? "Webhook"
      : pollingAccounts < connectedAccounts
        ? "Webhook + polling"
        : "Polling";
  const emailSyncMethod = [deliveryMethod, ngrokStatusLabel].filter(Boolean).join(" · ") || null;
  const ngrokTooltip = ngrokState === "online"
    ? "The ngrok tunnel is up, so provider webhooks can reach this app."
    : ngrokState === "mismatch"
      ? "ngrok is running, but its public address is different from the webhook address configured for this app."
      : ngrokState === "offline"
        ? "ngrok is not running, so provider webhooks cannot reach this app."
        : null;
  const emailSyncStatusLabel = emailSyncState === "syncing"
    ? syncingIds.size > 0
      ? `Syncing ${syncingIds.size} ${syncingIds.size === 1 ? "account" : "accounts"}`
      : "Syncing"
    : emailSyncState === "active"
      ? syncAgeLabel ?? "Waiting for sync"
      : emailSyncState === "no_account"
        ? "No account"
        : emailSyncState === "stopped"
          ? "Sync paused"
          : emailSyncState === "error"
            ? "Needs attention"
            : emailSyncState === "offline"
              ? "Unavailable"
            : "Checking";
  const emailSyncLabel = emailSyncMethod && emailSyncState !== "offline" && emailSyncState !== "loading"
    ? `${emailSyncStatusLabel} · ${emailSyncMethod}`
    : emailSyncStatusLabel;
  const emailSyncTooltip = autoSyncOffline
    ? "The app cannot reach the email sync service."
    : autoSyncStatus?.last_error
    ?? (emailSyncState === "active"
      ? deliveryMethod === "Webhook"
        ? "New mail arrives through the webhook. Polling stays off while delivery is working."
        : deliveryMethod === "Polling"
          ? `The webhook is not delivering mail, so each account is polled every ${Math.round((autoSyncStatus?.interval_seconds ?? 120) / 60)} minute(s).`
          : `Accounts with a working webhook stay on webhook only. The others are polled every ${Math.round((autoSyncStatus?.interval_seconds ?? 120) / 60)} minute(s).`
      : emailSyncLabel);
  const pollMinutes = Math.max(1, Math.round((autoSyncStatus?.interval_seconds ?? 120) / 60));
  const agoLabel = (value: string | null | undefined) => {
    const time = value ? Date.parse(value) : Number.NaN;
    if (!Number.isFinite(time)) return null;
    const minutes = Math.floor(Math.max(0, Date.now() - time) / 60_000);
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    return hours < 24 ? `${hours}h ago` : `${Math.floor(hours / 24)}d ago`;
  };
  const accountSyncLines = autoSyncOffline ? [] : (autoSyncStatus?.mailboxes ?? []).map((mailbox) => {
    const synced = agoLabel(mailbox.last_sync_completed_at);
    const syncedText = synced ? ` Last synced ${synced}.` : " Not synced yet.";
    const error = mailbox.last_error?.replace(/^\d{3}:\s*/, "");
    let state: string;
    if (mailbox.needs_reconnect) {
      state = `Not syncing — ${error ?? "the saved sign-in was rejected; reconnect this account"}`;
    } else if (mailbox.state === "running") {
      state = "Syncing now…";
    } else if (error) {
      state = `Sync failed: ${error}`;
    } else if (mailbox.sync_method === "polling") {
      state = `Checked every ${pollMinutes} min because: ${mailbox.polling_reason ?? "the webhook is not delivering mail"}.${syncedText}`;
    } else {
      state = `Live — new mail arrives instantly via webhook.${syncedText}`;
    }
    return `• ${mailbox.email_address}\t${state}`;
  });
  const emailSyncTooltipWithNgrok = [
    `Email sync — ${emailSyncLabel}`,
    emailSyncTooltip !== emailSyncLabel ? emailSyncTooltip : null,
    ...accountSyncLines,
    ngrokTooltip ? `ngrok: ${ngrokTooltip}` : null,
    autoSyncStatus?.last_check_at ? `Last automatic check ${agoLabel(autoSyncStatus.last_check_at)}.` : null,
  ].filter(Boolean).join("\n");
  const aiTooltip = [
    `AI model — ${aiStatusOffline ? "Unavailable" : aiLabel}`,
    aiStatus ? `Provider: ${aiStatus.provider === "ollama" ? "Ollama (local)" : aiStatus.provider === "gemini" ? "Google Gemini" : "OpenAI"} · Model: ${aiStatus.model}` : null,
    aiStatusOffline
      ? "Can't reach the app's backend to check the AI model, so new emails can't be classified."
      : !aiStatus
        ? "Checking the AI model…"
        : aiStatus.ready
          ? "Ready — new emails are classified automatically."
          : `Not ready: ${aiStatus.reason ?? "the selected model can't be used right now"} New emails won't be classified until this is fixed.`,
    "Click to open AI settings.",
  ].filter(Boolean).join("\n");
  const aiProviderLabel = aiStatus?.provider === "openai"
    ? "OpenAI"
    : aiStatus?.provider === "gemini"
      ? "Gemini"
      : aiStatus?.provider === "ollama"
        ? "Ollama"
        : "";
  const aiModelLabel = aiStatus
    ? `${aiProviderLabel} · ${aiStatus.model}`
    : aiStatusOffline
      ? "Unavailable"
      : "Checking";

  const renderSidebarAccounts = () => (
    <div className="sidebar-account-list">
      {orderedMailboxes.map((box) => {
        const unread = mailboxUnreadCounts[box.id] ?? 0;
        const active = activePage === "inbox" && selectedMailboxId === box.id;
        return <div
          className={`sidebar-account-wrap ${draggedAccountId === box.id ? "is-dragging" : ""}`}
          draggable
          onDragStart={(event) => onAccountDragStart(event, box.id)}
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => onAccountDrop(event, box.id)}
          onDragEnd={() => setDraggedAccountId(null)}
          key={box.id}
        >
          <button type="button" className={`sidebar-account-row ${active ? "active" : ""}`} onClick={() => selectMailboxFolder(box.id, "all")}>
            <span className={`sidebar-account-avatar provider-${box.provider}`} style={{ "--account-color": accountColor(box) } as CSSProperties}>{providerMark(box.provider)}</span>
            <span className="sidebar-account-email">{box.email_address}</span>
            {unread > 0 && <span className="sidebar-account-count is-unread" aria-label={`${unread} unread`}>+{unread}</span>}
          </button>
        </div>;
      })}
      {!orderedMailboxes.length && <p className="sidebar-empty">Add an account to start.</p>}
    </div>
  );

  if (settingsOpen) {
    return (
      <>
      <InterviewAlarm mailboxes={mailboxes} />
      <SettingsPage
        section={settingsSection}
        onSectionChange={setSettingsSection}
        sessionUser={sessionUser}
        onSignOut={onSignOut}
        onClose={() => setSettingsOpen(false)}
        themePref={themePref}
        onThemeChange={chooseTheme}
        viewMode={viewMode}
        onViewModeChange={chooseViewMode}
        inboxType={inboxType}
        onInboxTypeChange={chooseInboxType}
        readingPane={readingPane}
        onReadingPaneChange={chooseReadingPane}
        conversationView={conversationView}
        onConversationViewChange={chooseConversationView}
        fontFamily={fontFamily}
        onFontFamilyChange={chooseFontFamily}
        fontSize={fontSize}
        onFontSizeChange={chooseFontSize}
        mailboxes={orderedMailboxes}
        unusedTraining={unusedTraining}
        updatingPrompt={updatingPrompt}
        onUpdatePrompt={onUpdatePrompt}
        onTrainingDeleted={refreshPromptStatus}
        onAiSettingsSaved={refreshAiStatus}
        onAddAccount={() => {
          setSettingsOpen(false);
          setConnectEmail("");
          setConnectProvider("google");
          setDialogStep("type");
          setAddAccountOpen(true);
        }}
        onAccountColor={onAccountColor}
        onRemoveAccount={onDisconnect}
        onReconnectAccount={onReconnect}
        accountSyncDetails={accountSyncDetails}
        syncingIds={syncingIds}
        reclassifyingIds={reclassifyingIds}
        onSyncAccount={(id) => onSync(id, false)}
        onStopSyncAccount={(id) => onStopSync(id, false)}
        onReclassifyAccount={(id) => onReclassify(id, false)}
        onStopReclassifyAccount={(id) => onStopReclassify(id, false)}
        accountStatus={mailboxActions}
        reclassifyProgress={reclassifyProgress}
      />
      </>
    );
  }

  return (
    <div
      ref={shellRef}
      className={`app-shell view-${viewMode} reading-${readingPane.replace("_", "-")}${activePage === "calendar" ? " calendar-open" : ""}${activePage === "monitoring" || activePage === "statistics" ? " monitoring-open" : ""}${activePage === "statistics" ? " statistics-open" : ""}${tableDialogOpen ? " table-dialog-open" : ""}${sidebarCollapsed ? " sidebar-collapsed" : ""}${resizing ? ` is-resizing resizing-${resizing}` : ""}`}
      style={
        {
          "--accounts-w": sidebarCollapsed ? "3.55rem" : `${accountsW}px`,
          "--accounts-gap": sidebarCollapsed ? "0px" : "6px",
          "--list-w": `${listW}px`,
          "--list-h": `${listH}px`,
        } as CSSProperties
      }
    >
      <InterviewAlarm mailboxes={mailboxes} />
      <InterviewEmailAlert
        mailboxes={mailboxes}
        refreshKey={selectedId}
        suppressed={systemAlertOpen && visibleIssues.length > 0}
        onOpenEmail={(id) => void onOpenEmail(id)}
        onOpenCalendar={openCalendar}
      />
      <SystemAlert
        issues={visibleIssues}
        open={systemAlertOpen}
        checking={recheckingStatus}
        onDismiss={dismissSystemAlert}
        onReopen={reopenSystemAlert}
        onRecheck={() => void recheckStatus()}
      />
      <aside className={`pane-accounts${sidebarCollapsed ? " is-collapsed" : ""}`} ref={accountsPaneRef}>
        {sidebarCollapsed ? (
          <nav className="sidebar-rail" aria-label="Mailbox navigation">
            <div className="sidebar-rail-main">
              <button type="button" className="sidebar-rail-btn" aria-label="Home" data-tooltip="Home" data-tooltip-hint="All mail from every account" data-tooltip-placement="right" onClick={() => selectMailboxFolder(null, "all")}>
                <SidebarIcon name="home" />
              </button>
              <button type="button" className={`sidebar-rail-btn${activePage === "inbox" && selectedMailboxId == null ? " active" : ""}`} aria-label={inboxCount > 0 ? `Emails, ${inboxCount} unread` : "Emails"} data-tooltip="Emails" data-tooltip-hint={inboxCount > 0 ? `${inboxCount.toLocaleString()} unread messages` : "All mail from every folder"} data-tooltip-placement="right" onClick={() => selectMailboxFolder(null, "all")}>
                <SidebarIcon name="inbox" />
                {inboxCount > 0 && <span className="sidebar-rail-badge">{inboxCount > 99 ? "+99" : `+${inboxCount}`}</span>}
              </button>
              <button type="button" className={`sidebar-rail-btn${activePage === "calendar" ? " active" : ""}`} aria-label="Calendar" data-tooltip="Calendar" data-tooltip-hint="Interviews and meetings" data-tooltip-placement="right" onClick={openCalendar}>
                <SidebarIcon name="calendar" />
              </button>
              {sessionUser.is_admin && <button type="button" className={`sidebar-rail-btn${activePage === "monitoring" ? " active" : ""}`} aria-label="Monitoring" data-tooltip="Monitoring" data-tooltip-hint="Service health and processing queues" data-tooltip-placement="right" onClick={openMonitoring}>
                <SidebarIcon name="monitoring" />
              </button>}
              {sessionUser.is_admin && <button type="button" className={`sidebar-rail-btn${activePage === "statistics" ? " active" : ""}`} aria-label="Statistics" data-tooltip="Statistics" data-tooltip-hint="Email categories and recruiting outcomes" data-tooltip-placement="right" onClick={openStatistics}>
                <SidebarIcon name="statistics" />
              </button>}
              <button type="button" className={`sidebar-rail-btn${activePage === "inbox" && label !== "all" ? " active" : ""}`} aria-label="Categories" data-tooltip="Categories" data-tooltip-hint="Opens the sidebar to filter by category" data-tooltip-placement="right" onClick={() => { setActivePage("inbox"); setSidebarSections((current) => ({ ...current, categories: true })); setSidebarCollapsed(false); try { localStorage.setItem(SIDEBAR_COLLAPSED_KEY, "0"); } catch { /* ignore */ } }}>
                <svg className="sidebar-nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 3.5h6" /><path d="M10 3.5v5.2L7 12.2h10L14 8.7V3.5" /><path d="M12 12.2v8.3" /></svg>
              </button>
            </div>
            <button
              type="button"
              className="sidebar-rail-btn sidebar-rail-menu"
              aria-label="Show sidebar"
              data-tooltip="Show sidebar"
              data-tooltip-hint="Expand accounts and categories"
              data-tooltip-placement="right"
              onClick={() => {
                setSidebarCollapsed(false);
                try { localStorage.setItem(SIDEBAR_COLLAPSED_KEY, "0"); } catch { /* ignore */ }
              }}
            >
              <svg className="sidebar-nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16" /></svg>
            </button>
          </nav>
        ) : <>
        <div className="accounts-top">
          <div className="sidebar-brand-row">
            <button type="button" className="sidebar-brand" onClick={() => selectMailboxFolder(null, "all")} aria-label="Auto Email Check home">
              <img src="/favicon.svg" alt="" />
              <span>Auto Email Check</span>
            </button>
            <div className="sidebar-brand-actions">
              <button
                type="button"
                className="sidebar-sync-trigger"
                aria-label="Hide sidebar"
                data-tooltip="Hide sidebar"
                data-tooltip-hint="Collapse this panel"
                data-tooltip-placement="bottom"
                onClick={() => {
                  setSyncMenuOpen(false);
                  setSidebarCollapsed(true);
                  try { localStorage.setItem(SIDEBAR_COLLAPSED_KEY, "1"); } catch { /* ignore */ }
                }}
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V7.5a4 4 0 0 1 7.6-1.7" /></svg>
              </button>
            <div className="sidebar-sync-menu" ref={syncMenuRef}>
              <button
                type="button"
                className={`sidebar-sync-trigger ${syncMenuOpen ? "active" : ""} ${syncingIds.size ? "syncing" : ""}`}
                aria-label="Open sync menu"
                data-tooltip={syncMenuOpen ? undefined : "Sync"}
                data-tooltip-hint={syncMenuOpen ? undefined : "Email or calendar"}
                data-tooltip-placement="bottom"
                aria-haspopup="menu"
                aria-expanded={syncMenuOpen}
                onClick={() => setSyncMenuOpen((open) => !open)}
              >
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5" /><path d="M6.1 8.2A7 7 0 0 1 18.8 7L20 12M4 12l1.2 5A7 7 0 0 0 17.9 15.8" /></svg>
              </button>
              {syncMenuOpen && <div className="sidebar-sync-popover" role="menu" aria-label="Sync options">
                <button type="button" role="menuitem" disabled={!orderedMailboxes.some((mailbox) => mailbox.is_active) || syncingIds.size > 0} onClick={() => void onSyncAllEmails()}>
                  <span className="sidebar-sync-option-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="3" /><path d="m4 7 8 6 8-6" /></svg></span>
                  <span><strong>Email sync</strong><small>{syncingIds.size > 0 ? emailSyncStatusLabel : "Sync all connected accounts"}</small></span>
                </button>
                <button type="button" role="menuitem" onClick={onSyncCalendar}>
                  <span className="sidebar-sync-option-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4.5" width="18" height="16" rx="3" /><path d="M7 2.5v4M17 2.5v4M3 9h18" /></svg></span>
                  <span><strong>Calendar sync</strong><small>Refresh calendar events</small></span>
                </button>
              </div>}
            </div>
            </div>
          </div>
        </div>
        <nav className="sidebar-tree" aria-label="Mailbox navigation">
          <section className="sidebar-tree-section">
            <div className="sidebar-section-heading">
              <button type="button" className={`sidebar-disclosure ${sidebarSections.inbox ? "open" : ""}`} aria-label={`${sidebarSections.inbox ? "Collapse" : "Expand"} Emails accounts`} aria-expanded={sidebarSections.inbox} onClick={() => toggleSidebarSection("inbox")}><span>›</span></button>
              <button type="button" className={`sidebar-nav-row ${activePage === "inbox" && selectedMailboxId == null ? "active" : ""}`} onClick={() => { setSidebarSections((current) => ({ ...current, inbox: true })); selectMailboxFolder(null, "all"); }}><SidebarIcon name="inbox" /><span>Emails</span>{inboxCount > 0 && <span className="sidebar-row-count is-unread" aria-label={`${inboxCount} unread`}>+{inboxCount}</span>}</button>
            </div>
            {sidebarSections.inbox && renderSidebarAccounts()}
          </section>

          <button type="button" className={`sidebar-nav-row sidebar-primary-row ${activePage === "calendar" ? "active" : ""}`} onClick={openCalendar}><SidebarIcon name="calendar" /><span>Calendar</span></button>
          {sessionUser.is_admin && <button type="button" className={`sidebar-nav-row sidebar-primary-row ${activePage === "monitoring" ? "active" : ""}`} onClick={openMonitoring}><SidebarIcon name="monitoring" /><span>Monitoring</span></button>}
          {sessionUser.is_admin && <button type="button" className={`sidebar-nav-row sidebar-primary-row ${activePage === "statistics" ? "active" : ""}`} onClick={openStatistics}><SidebarIcon name="statistics" /><span>Statistics</span></button>}

          <section className="sidebar-tree-section">
            <div className="sidebar-section-heading">
              <button type="button" className={`sidebar-disclosure ${sidebarSections.categories ? "open" : ""}`} aria-label={`${sidebarSections.categories ? "Collapse" : "Expand"} categories`} aria-expanded={sidebarSections.categories} onClick={() => toggleSidebarSection("categories")}><span>›</span></button>
              <button type="button" className={`sidebar-nav-row ${label !== "all" ? "active" : ""}`} onClick={() => toggleSidebarSection("categories")}><SidebarIcon name="categories" /><span>Categories</span></button>
            </div>
            {sidebarSections.categories && <div className="sidebar-category-list">
              <button type="button" className={`sidebar-filter-row ${label === "all" ? "active" : ""}`} onClick={() => { setActivePage("inbox"); selectLabel("all"); }}><span>All categories</span>{renderCountBadge(allCategoriesTally, "sidebar-filter-count")}</button>
              {CLASSIFY_LABELS.map((item) => <button type="button" className={`sidebar-filter-row ${label === item ? "active" : ""}`} onClick={() => { setActivePage("inbox"); selectLabel(item); }} key={item}><span>{CLASSIFY_LABEL_TITLES[item]}</span>{renderCountBadge(labelTallies[item], "sidebar-filter-count")}</button>)}
            </div>}
          </section>
        </nav>

        <div className="sidebar-footer">
          <div className="sidebar-status-panel" aria-label="Service status">
            <button type="button" className="sidebar-health-row email-sync-status" data-state={emailSyncState} data-ngrok={ngrokState ?? "unknown"} data-tooltip={emailSyncTooltipWithNgrok} data-tooltip-full data-tooltip-placement="top" aria-label={`Email sync: ${emailSyncLabel}. Open account settings`} onClick={() => openSettings("accounts")}>
              <span className="sidebar-health-dot" aria-hidden="true" />
              <span className="sidebar-health-copy">
                <span className="sidebar-health-name">Email sync</span>
                <span className="sidebar-health-value" role="status" aria-live="polite">
                  <span className="sidebar-health-status">{emailSyncStatusLabel}</span>
                  {emailSyncMethod && emailSyncState !== "offline" && emailSyncState !== "loading" && (
                    <span className={`sidebar-sync-method ngrok-${ngrokState ?? "unknown"}`}>{emailSyncMethod}</span>
                  )}
                </span>
              </span>
            </button>
            <button type="button" className="sidebar-health-row ai-model-status" data-state={aiReady ? "active" : "error"} data-tooltip={aiTooltip} data-tooltip-full data-tooltip-placement="top" aria-label={sessionUser.is_admin ? `${aiTitle}. Open AI settings` : aiTitle} onClick={() => openSettings(sessionUser.is_admin ? "ai" : "appearance")}>
              <span className="sidebar-health-dot" aria-hidden="true" />
              <span className="sidebar-health-copy"><span className="sidebar-health-name">AI model</span><span className="sidebar-health-value">{aiModelLabel}</span></span>
              <span className="sr-only" role="status" aria-live="polite">{aiLabel}</span>
            </button>
          </div>
          <div className="sidebar-signin-row">
            <button type="button" className="sidebar-signin-user" data-tooltip="Account & settings" data-tooltip-placement="top" aria-label={`Signed in as ${sessionUser.email}. Open account settings`} onClick={() => openSettings("signin")}>
              <span className="sidebar-signin-avatar" aria-hidden="true">{(sessionUser.display_name || sessionUser.email).charAt(0).toUpperCase()}</span>
              <span className="sidebar-signin-copy">
                <span className="sidebar-signin-name">
                  {sessionUser.display_name || sessionUser.email.split("@")[0]}
                  <span className={`sidebar-signin-role${sessionUser.is_admin ? " is-admin" : ""}`}>{sessionUser.is_admin ? "Admin" : "Member"}</span>
                </span>
                <span className="sidebar-signin-email">{sessionUser.email}</span>
              </span>
            </button>
            <button type="button" className="sidebar-signin-out" data-tooltip="Sign out" data-tooltip-placement="top" aria-label="Sign out" onClick={onSignOut}>
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3" /><path d="M10 16l-4-4 4-4" /><path d="M6 12h10" /></svg>
            </button>
          </div>
        </div>
        </>}
      </aside>

      <button
        type="button"
        className="pane-splitter accounts-splitter"
        role="separator"
        aria-label="Resize accounts pane"
        aria-hidden={sidebarCollapsed}
        tabIndex={sidebarCollapsed ? -1 : 0}
        aria-orientation="vertical"
        aria-valuenow={accountsW}
        onPointerDown={onSplitterPointerDown("accounts")}
        onPointerMove={onSplitterPointerMove}
        onPointerUp={onSplitterPointerUp}
        onPointerCancel={onSplitterPointerUp}
        onKeyDown={onSplitterKeyDown("accounts")}
      />

      {activePage === "monitoring" && sessionUser.is_admin && (
        <MonitoringPage active />
      )}

      {activePage === "statistics" && sessionUser.is_admin && (
        <StatisticsPage active mailboxes={mailboxes} initialMailboxId={selectedMailboxId} />
      )}

      {activePage === "inbox" && <>
      <section className={`pane-list view-${viewMode}`}>
        <header className="list-header">
          <div className="list-title">
            <div className="list-title-row">
              <h2>
                {selectedMailbox ? selectedMailbox.email_address : "All accounts"}
              </h2>
              <div className="list-header-actions">
                <span
                  className="list-header-icon-wrap"
                  data-tooltip={markingAllRead ? "Marking as read…" : scopeUnread === 0 ? "All caught up" : "Mark all as read"}
                  data-tooltip-hint={markingAllRead ? "This list is being updated" : scopeUnread === 0 ? "Nothing unread in this list" : "Marks unread mail in this list"}
                  data-tooltip-placement="bottom"
                >
                  <button
                    type="button"
                    className="list-header-icon"
                    aria-label="Mark all as read"
                    disabled={scopeUnread === 0 || markingAllRead}
                    onClick={() => setMarkAllOpen(true)}
                  >
                    <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5.5" width="13" height="11" rx="2" /><path d="m4.2 8 5.3 3.6L14.8 8" /><path d="m14.2 15.2 2.2 2.2 4.6-4.8" /></svg>
                  </button>
                </span>
              </div>
            </div>
          </div>
          <form
            className="list-search"
            onSubmit={(event: FormEvent) => {
              event.preventDefault();
              setSearchQuery(searchInput.trim());
            }}
          >
            <input
              className="list-search-input"
              type="search"
              placeholder="Search mail"
              aria-label="Search mail"
              value={searchInput}
              onChange={(event) => setSearchInput(event.target.value)}
            />
            {searchInput ? (
              <button
                type="button"
                className="list-search-clear"
                aria-label="Clear search"
                onClick={() => {
                  setSearchInput("");
                  setSearchQuery("");
                }}
              >
                ×
              </button>
            ) : null}
          </form>
        </header>

        {label === "interview_scheduled" && (
          <div className="subtype-filter" role="group" aria-label="Interview Scheduled subtype">
            <label className="subtype-filter-label" htmlFor="interview-subtype-filter">Interview subtype</label>
            <CompactSelect
              id="interview-subtype-filter"
              className="subtype-filter-select"
              value={interviewSubtype}
              onChange={(event) => selectInterviewSubtype(event.target.value as "all" | InterviewSubtype)}
            >
              <option value="all">All subtypes</option>
              {(Object.entries(INTERVIEW_SUBTYPE_TITLES) as [InterviewSubtype, string][]).map(([value, title]) => (
                <option key={value} value={value}>{title}</option>
              ))}
            </CompactSelect>
            {!loading && <span className="subtype-filter-count" role="status">{filteredTotal} {conversationView ? (filteredTotal === 1 ? "conversation" : "conversations") : (filteredTotal === 1 ? "email" : "emails")}</span>}
          </div>
        )}

        {banner && (
          <div className="banner inline" role="status">
            <span>{banner}</span>
            <button type="button" onClick={() => setBanner(null)}>
              Dismiss
            </button>
          </div>
        )}
        {loading && <p className="hint pad">Loading…</p>}
        {error && <p className="error pad">{error}</p>}

        {viewMode === "table" && <p className="table-view-hint">{readingPane === "no_split" ? "Double-click an email to open it." : "Select an email to read it."}</p>}
        {viewMode === "table" && (
          <div className="table-header table-columns" aria-hidden="true">
            <span>From</span>
            <span>Subject</span>
            <span>Category</span>
            <span className="table-folder">Folder</span>
            <span className="table-status">Status</span>
            <span className="table-date">Date</span>
          </div>
        )}
        <div
          className={`message-scroll view-${viewMode}`}
          ref={scrollRef}
          tabIndex={0}
          role="region"
          aria-label="Messages; use Up and Down arrows to select"
          onKeyDown={onMessageListKeyDown}
        >
          {!loading && listRows.length > 0 && (
            <div
              className="virtual-list"
              style={{ height: listVirtualizer.getTotalSize() }}
            >
              {virtualItems.map((virtualRow) => {
                const row = listRows[virtualRow.index];
                if (!row) return null;
                const style: CSSProperties = {
                  transform: `translateY(${virtualRow.start}px)`,
                };
                if (row.kind === "group") {
                  const collapsed = collapsedDates.has(row.key);
                  return (
                    <div
                      key={virtualRow.key}
                      data-index={virtualRow.index}
                      ref={listVirtualizer.measureElement}
                      className="virtual-row"
                      style={style}
                    >
                      <button
                        type="button"
                        className="msg-group-head"
                        onClick={() => toggleDateGroup(row.key)}
                        aria-expanded={!collapsed}
                      >
                        <span className="date-chevron" aria-hidden="true">
                          {collapsed ? "▸" : "▾"}
                        </span>
                        <span className="date-heading">{row.heading}</span>
                      </button>
                    </div>
                  );
                }
                const email = row.email;
                const when = formatItemTimeAndDay(email.received_at);
                const rowMailbox = selectedMailboxId == null ? mailboxById.get(email.mailbox_id) : undefined;
                const accountAddress = rowMailbox?.email_address ?? "";
                return (
                  <div
                    key={virtualRow.key}
                    data-index={virtualRow.index}
                    ref={listVirtualizer.measureElement}
                    className="virtual-row"
                    style={style}
                  >
                    <MessageRow
                      email={email}
                      viewMode={viewMode}
                      active={selectedId === email.id}
                      accountAddress={accountAddress}
                      accountTint={rowMailbox ? accountColor(rowMailbox) : undefined}
                      when={when}
                      folder={mailFolderOf(email)}
                      folderTitle={MAIL_FOLDER_TITLES[mailFolderOf(email)]}
                      onOpen={openMessage}
                      onDoubleOpen={openMessageDirect}
                      onPrefetch={prefetchMessage}
                    />
                  </div>
                );
              })}
            </div>
          )}
          {!loading && !emails.length && mailboxes.length > 0 && (
            <p className="hint pad">
              {searchQuery
                ? "No emails match this search."
                : label === "interview_scheduled" && interviewSubtype !== "all"
                  ? `No ${INTERVIEW_SUBTYPE_TITLES[interviewSubtype].toLowerCase()} interviews match these filters.`
                : folder === "all"
                  ? "No classified emails yet. Sync an account."
                  : `No emails in ${MAIL_FOLDER_TITLES[folder]}. Sync to refresh folder labels.`}
            </p>
          )}
          {!loading && !mailboxes.length && (
            <p className="hint pad">Add an account to start.</p>
          )}
          {loadingMore && <p className="hint pad loading-more">Loading more…</p>}
        </div>
      </section>

      <button
        type="button"
        className="pane-splitter reader-splitter"
        role="separator"
        aria-label={readingPane === "below" ? "Resize message list height" : "Resize message list width"}
        aria-orientation={readingPane === "below" ? "horizontal" : "vertical"}
        aria-valuenow={readingPane === "below" ? listH : listW}
        onPointerDown={onSplitterPointerDown("list")}
        onPointerMove={onSplitterPointerMove}
        onPointerUp={onSplitterPointerUp}
        onPointerCancel={onSplitterPointerUp}
        onKeyDown={onSplitterKeyDown("list")}
      />
      </>}

      {activePage === "inbox" && readingPane === "no_split" && tableDialogOpen && (
        <div className="table-dialog-backdrop" role="presentation" onClick={closeTableDialog} />
      )}
      <section
        className="pane-reader"
        ref={readerDialogRef}
        role={activePage === "inbox" && readingPane === "no_split" && tableDialogOpen ? "dialog" : undefined}
        aria-modal={activePage === "inbox" && readingPane === "no_split" && tableDialogOpen ? true : undefined}
        aria-label={activePage === "calendar" ? "Calendar" : readingPane === "no_split" && tableDialogOpen ? selected?.subject || "Email" : undefined}
      >
        {calendarMounted && (
          <div className={`calendar-page-host${activePage === "calendar" ? "" : " is-hidden"}`} hidden={activePage !== "calendar"} aria-hidden={activePage !== "calendar"}>
            <CalendarPage
              mailboxes={orderedMailboxes}
              refreshKey={calendarRefreshKey}
              active={activePage === "calendar"}
              onClose={() => setActivePage("inbox")}
            />
          </div>
        )}
        {activePage !== "calendar" && <>
        {readingPane === "no_split" && tableDialogOpen && (
          <div className="table-dialog-toolbar">
            <span>Email</span>
            <button type="button" className="dialog-close" ref={readerCloseRef} onClick={closeTableDialog} aria-label="Close email">×</button>
          </div>
        )}
        {!selected && !loadingDetail && (
          <div className="reader-empty">
            <p>Select a message to read</p>
          </div>
        )}
        {!selected && loadingDetail && <div className="reader-empty"><p>Opening…</p></div>}
        {selected && (
          <article className="reader-article">
            <h1 id="reader-title">{selected.subject || "(no subject)"}</h1>
            <div className={showConversation ? "reader-card is-conversation" : "reader-card"} ref={conversationScrollRef}>
              {earlierThread.map((message) => renderThreadMessage(message))}
              {showConversation && selected && !selectedThreadOpen ? (
                renderThreadMessage(selected)
              ) : (
              <section className={showConversation ? "thread-card is-open is-selected" : "thread-selected"}>
              <div className="reader-meta">
                <div className="reader-people">
                  <span className={`avatar soft label-${selected.label}`}>
                    {initialsFrom(senderName(selected.sender))}
                  </span>
                  <div>
                    <div className="reader-from-line">
                      <strong>{senderName(selected.sender)}</strong>
                      {selectedSenderAddress && selectedSenderAddress !== senderName(selected.sender) ? (
                        <span className="reader-address">&lt;{selectedSenderAddress}&gt;</span>
                      ) : null}
                    </div>
                    <div className="reader-chips">
                      {readerAccount ? (
                        <span
                          className="reader-chip reader-to-chip"
                          title={readerAccount}
                          style={readerMailbox ? ({ "--account-color": accountColor(readerMailbox) } as CSSProperties) : undefined}
                        >
                          <span className="reader-to-dot" aria-hidden="true" />
                          <span className="reader-to-label">To</span>
                          <span className="to-address">{readerAccount}</span>
                        </span>
                      ) : null}
                      <span className={`folder-pill folder-${mailFolderOf(selected)}`}>
                        {MAIL_FOLDER_TITLES[mailFolderOf(selected)]}
                      </span>
                    </div>
                  </div>
                </div>
                <div className="reader-side">
                  <div className="reader-time-line">
                    <time className="reader-time" title={selected.received_at ?? undefined}>
                      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></svg>
                      {formatReaderTime(selected.received_at)}
                    </time>
                    {showConversation && (
                      <button
                        type="button"
                        className="thread-collapse"
                        aria-label="Collapse message"
                        data-tooltip="Collapse message"
                        onClick={() => setOpenThreadIds((ids) => ids.filter((item) => item !== selected.id))}
                      >
                        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 14.5 6-6 6 6" /></svg>
                      </button>
                    )}
                  </div>
                  <div className="reader-controls">
                    <CompactSelect
                      className={`label-select label-${selected.label}`}
                      containerClassName={`category-compact-select label-${selected.label}`}
                      value={selected.label}
                      disabled={savingLabel || pendingLabel != null}
                      aria-label="Email category"
                      onChange={(event) => onChangeCategory(event.target.value as EmailLabel)}
                    >
                      {CLASSIFY_LABELS.map((item) => (
                        <option key={item} value={item}>
                          {CLASSIFY_LABEL_TITLES[item]}
                        </option>
                      ))}
                    </CompactSelect>
                    {selected.label === "interview_scheduled" && selected.interview_subtype && (
                      <CompactSelect
                        value={selected.interview_subtype}
                        disabled={savingLabel || pendingLabel != null}
                        aria-label="Interview subtype"
                        className="label-select label-interview_scheduled"
                        containerClassName="category-compact-select label-interview_scheduled"
                        onChange={async (event) => {
                          const subtype = event.target.value as InterviewSubtype;
                          const emailId = selected.id;
                          setSavingLabel(true);
                          try {
                            const updated = await updateEmailLabel(emailId, "interview_scheduled", true, subtype);
                            setSelected((prev) => prev?.id === emailId ? { ...prev, ...updated } : prev);
                            setEmails((prev) => {
                              const currentSubtype = filterRef.current.interviewSubtype;
                              if (currentSubtype !== "all" && currentSubtype !== updated.interview_subtype) {
                                return prev.filter((item) => item.id !== emailId);
                              }
                              return prev.map((item) => item.id === emailId ? { ...item, ...updated } : item);
                            });
                            setUnusedTraining((count) => count + 1);
                            void refreshPromptStatus();
                          } catch (err) {
                            setBanner(err instanceof Error ? err.message : "Could not update interview subtype");
                          } finally {
                            setSavingLabel(false);
                          }
                        }}
                      >
                        {(Object.entries(INTERVIEW_SUBTYPE_TITLES) as [InterviewSubtype, string][]).map(([value, title]) => (
                          <option key={value} value={value}>{title}</option>
                        ))}
                      </CompactSelect>
                    )}
                  </div>
                  <div className="reader-secondary">
                    <label className={`train-check train-switch${saveTraining ? " is-on" : ""}`} data-tooltip="Use category changes to improve the AI" data-tooltip-placement="bottom">
                      <input
                        type="checkbox"
                        role="switch"
                        checked={saveTraining}
                        onChange={(event) => setSaveTraining(event.target.checked)}
                      />
                      <span>Save as training data</span>
                    </label>
                  </div>
                </div>
              </div>
              {selected.label === "application_action_required" ? (
                <ApplicationActionCopies
                  subject={selected.subject}
                  html={selected.body_html}
                  text={selected.body_text}
                  snippet={selected.snippet}
                />
              ) : null}
              <EmailBody
                html={selected.body_html}
                text={selected.body_text}
                snippet={selected.snippet}
              />
              </section>
              )}
              {laterThread.map((message) => renderThreadMessage(message))}
              {!replyOpen && (
                <div className="reply-action-row">
                  <button type="button" className="reply-pill" onClick={() => { setScheduleAt(""); setReplyInstruction(""); setReplyOpen(true); }}>
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 8 4 12l5 4" /><path d="M4 12h9.5a5.5 5.5 0 0 1 5.5 5.5V19" /></svg>
                    <span>Reply</span>
                  </button>
                </div>
              )}
              {replyOpen && (
                <section className="reply-composer" aria-label="Reply composer">
                  <header>
                    <strong>Reply</strong>
                    <span>From {replyMailbox?.email_address ?? "selected account"} to {selected.sender}</span>
                  </header>
                  <textarea autoFocus value={replyText} onChange={(event) => setReplyText(event.target.value)} placeholder="Write your reply" />
                  <form className="reply-ai-prompt" onSubmit={(event) => { event.preventDefault(); void draftAiReply(); }}>
                    <span aria-hidden="true">✦</span>
                    <input
                      aria-label="Describe your reply"
                      value={replyInstruction}
                      onChange={(event) => setReplyInstruction(event.target.value)}
                      placeholder="Describe your message"
                      maxLength={2000}
                      disabled={aiDrafting}
                    />
                    <button type="submit" disabled={aiDrafting} aria-label="Generate reply from description">
                      {aiDrafting ? "Drafting…" : "Generate"}
                    </button>
                  </form>
                  <footer>
                    <div className="reply-compose-actions">
                      <div className="send-split"><button type="button" className="primary-btn" disabled={sendingReply || !replyText.trim() || !replyMailbox} onClick={() => { setScheduleAt(""); void sendReply(); }}>{sendingReply ? "Sending…" : "Send"}</button><button type="button" className="primary-btn send-menu" aria-label="Schedule send" disabled={sendingReply || !replyMailbox} onClick={() => setScheduleDialogOpen(true)}>◷</button></div>
                      <label className="reply-attach">📎 <span>Attach</span><input type="file" multiple onChange={(event) => setReplyFiles(Array.from(event.target.files ?? []))} /></label>
                      {replyFiles.length > 0 && <span>{replyFiles.map((file) => file.name).join(", ")}</span>}
                    </div>
                    <button className="reply-delete" type="button" aria-label="Delete reply draft" onClick={() => { setReplyOpen(false); setReplyText(""); setReplyInstruction(""); setReplyFiles([]); setScheduleAt(""); }}>🗑</button>
                  </footer>
                </section>
              )}
            </div>
          </article>
        )}
        </>}
      </section>

      {scheduleDialogOpen && (
        <div className="dialog-backdrop" role="presentation">
          <div className="dialog schedule-dialog" role="dialog" aria-modal="true" aria-labelledby="schedule-send-title">
            <header className="dialog-header"><div><h3 id="schedule-send-title">Schedule send</h3><p>Times shown in {(Intl.DateTimeFormat().resolvedOptions().timeZone || "Local time").replaceAll("_", " ")}</p></div><button type="button" className="dialog-close" aria-label="Close" onClick={() => setScheduleDialogOpen(false)}>×</button></header>
            <div className="schedule-presets"><button type="button" onClick={() => chooseSchedulePreset(1, 8)}><span>Tomorrow morning</span><small>8:00 AM</small></button><button type="button" onClick={() => chooseSchedulePreset(new Date().getHours() >= 13 ? 1 : 0, 13)}><span>{new Date().getHours() >= 13 ? "Tomorrow afternoon" : "This afternoon"}</span><small>1:00 PM</small></button><button type="button" onClick={() => chooseSchedulePreset((8 - new Date().getDay()) % 7 || 7, 8)}><span>Monday morning</span><small>8:00 AM</small></button></div>
            <div className="schedule-custom"><ModernDateTimePicker label="Custom date and time" value={scheduleAt} min={localDateTimeValue(new Date())} minuteStep={5} onChange={setScheduleAt} /></div>
            {scheduleAt && <div className="dialog-actions schedule-confirm"><button type="button" className="primary-btn" onClick={() => { setScheduleDialogOpen(false); void sendReply(); }}>Schedule send</button></div>}
          </div>
        </div>
      )}

      {markAllOpen && (
        <div className="dialog-backdrop" role="presentation">
          <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="mark-all-title">
            <header className="dialog-header">
              <h3 id="mark-all-title">Mark all as read</h3>
            </header>
            <div className="dialog-body">
              <p className="dialog-hint">
                {selectedMailbox
                  ? `Mark ${scopeUnread} new message${scopeUnread === 1 ? "" : "s"} as read for ${selectedMailbox.email_address}?`
                  : `Mark ${scopeUnread} new message${scopeUnread === 1 ? "" : "s"} as read for all accounts?`}
              </p>
              {markingAllRead && (
                <>
                  {markAllReadProgress && markAllReadProgress.total > 0 ? (
                    <>
                      <span
                        className="loading-bar is-determinate"
                        aria-hidden="true"
                        style={{ "--loading-bar-pct": `${Math.min(100, (markAllReadProgress.marked / markAllReadProgress.total) * 100)}%` } as CSSProperties}
                      >
                        <span />
                      </span>
                      <span className="loading-bar-count">
                        {markAllReadProgress.marked.toLocaleString()} / {markAllReadProgress.total.toLocaleString()}
                      </span>
                    </>
                  ) : (
                    <span className="loading-bar" aria-hidden="true"><span /></span>
                  )}
                </>
              )}
              <div className="dialog-actions">
                <button
                  type="button"
                  className="action-btn"
                  disabled={markingAllRead}
                  onClick={() => setMarkAllOpen(false)}
                >
                  No
                </button>
                <button
                  type="button"
                  className="primary-btn"
                  disabled={markingAllRead}
                  onClick={() => void confirmMarkAllRead()}
                >
                  {markingAllRead ? "Marking…" : "Yes"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {categoryPickerOpen && selected && (
        <CategoryPicker
          current={{ label: selected.label, subtype: selected.interview_subtype ?? null }}
          subject={selected.subject}
          onPick={onPickCategory}
          onClose={() => setCategoryPickerOpen(false)}
        />
      )}

      {pendingLabel && (
        <div className="dialog-backdrop" role="presentation">
          <div
            className="dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="train-confirm-title"
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                setPendingLabel(null);
                return;
              }
              if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) return;
              const target = event.target as HTMLElement;
              if (target.closest("select, .compact-select-menu, [role='listbox']")) return;
              const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>(".dialog-actions button:not(:disabled)"));
              if (!buttons.length) return;
              event.preventDefault();
              const at = buttons.indexOf(document.activeElement as HTMLButtonElement);
              const step = event.key === "ArrowDown" || event.key === "ArrowRight" ? 1 : -1;
              buttons[(at + step + buttons.length) % buttons.length].focus();
            }}
          >
            <header className="dialog-header">
              <h3 id="train-confirm-title">Prompt training</h3>
            </header>
            <div className="dialog-body">
              {pendingLabel.nextLabel === "interview_scheduled" && (
                <label className="dialog-email-label">
                  Interview event
                  <CompactSelect
                    value={pendingSubtype}
                    onChange={(event) => setPendingSubtype(event.target.value as InterviewSubtype | "")}
                    aria-label="Interview subtype"
                    required
                  >
                    <option value="">Choose a subtype</option>
                    {(Object.entries(INTERVIEW_SUBTYPE_TITLES) as [InterviewSubtype, string][]).map(([value, title]) => (
                      <option key={value} value={value}>{title}</option>
                    ))}
                  </CompactSelect>
                </label>
              )}
              <p className="dialog-change-summary">
                <span>Category</span>
                <strong>
                  {CLASSIFY_LABEL_TITLES[pendingLabel.nextLabel]}
                  {pendingLabel.nextLabel === "interview_scheduled" && pendingSubtype ? ` / ${INTERVIEW_SUBTYPE_TITLES[pendingSubtype]}` : ""}
                </strong>
              </p>
              <p className="dialog-hint">Use this email as prompt update training data?</p>
              <div className="dialog-actions">
                <button
                  type="button"
                  className="action-btn"
                  disabled={pendingLabel.nextLabel === "interview_scheduled" && !pendingSubtype}
                  onClick={() => void applyCategoryChange(false)}
                >
                  No
                </button>
                <button
                  type="button"
                  className="primary-btn"
                  autoFocus
                  disabled={pendingLabel.nextLabel === "interview_scheduled" && !pendingSubtype}
                  onClick={() => void applyCategoryChange(true)}
                >
                  Yes
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {addAccountOpen && (
        <div
          className="dialog-backdrop connect-dialog"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget) closeAddAccount();
          }}
        >
          <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="add-account-title">
            <header className="dialog-header">
              <h3 id="add-account-title">Add an account</h3>
              <button type="button" className="dialog-close" onClick={closeAddAccount} aria-label="Close">
                ×
              </button>
            </header>
            {dialogStep === "type" ? (
              <div className="dialog-body">
                <p className="dialog-hint">Choose your email type</p>
                <div className="account-type-grid">
                  <button
                    type="button"
                    className="account-type gmail"
                    onClick={() => chooseAccountType("google")}
                  >
                    <span className="account-type-name">Gmail</span>
                    <span className="account-type-sub">Google account</span>
                  </button>
                  <button
                    type="button"
                    className="account-type outlook"
                    onClick={() => chooseAccountType("microsoft")}
                  >
                    <span className="account-type-name">Outlook</span>
                    <span className="account-type-sub">Microsoft account</span>
                  </button>
                </div>
              </div>
            ) : (
              <form className="dialog-body" onSubmit={onConnectEmailSubmit}>
                <button type="button" className="text-btn back-link" onClick={() => setDialogStep("type")}>
                  ← {connectProvider === "google" ? "Gmail" : "Outlook"}
                </button>
                <p className="dialog-hint">
                  Enter your email, then press Enter to connect.
                </p>
                <label className="dialog-email-label">
                  Email
                  <input
                    type="email"
                    value={connectEmail}
                    onChange={(e) => setConnectEmail(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "ArrowDown" && connectEmail.includes("@")) {
                        e.preventDefault();
                        connectWithEmail();
                      }
                    }}
                    placeholder={
                      connectProvider === "google" ? "name@gmail.com" : "name@outlook.com"
                    }
                    required
                    autoFocus
                    autoComplete="email"
                  />
                </label>
                <button
                  type="submit"
                  className={`primary-btn ${connectProvider === "google" ? "google" : "outlook"}`}
                >
                  Connect
                </button>
              </form>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
