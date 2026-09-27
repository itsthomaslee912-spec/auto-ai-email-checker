import { CSSProperties, Dispatch, DragEvent, FormEvent, KeyboardEvent, PointerEvent, SetStateAction, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import EmailBody from "./components/EmailBody";
import MessageRow from "./components/MessageRow";
import SettingsPage, { type SettingsSection } from "./pages/SettingsPage";
import {
  checkAutoSyncNow,
  disconnectMailbox,
  eventsUrl,
  fetchAutoSyncStatus,
  fetchClassifyPromptStatus,
  fetchEmailDetail,
  fetchEmails,
  createAiReply,
  fetchReclassifyStatus,
  sendEmail,
  fetchMailboxes,
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
  type AutoSyncStatus,
  type MailFolder,
  type Mailbox,
  type Provider,
} from "./api";
import { CLASSIFY_LABELS, CLASSIFY_LABEL_TITLES, EMPTY_LABEL_COUNTS, INTERVIEW_SUBTYPE_TITLES } from "./labels";
import {
  applyFontFamily,
  applyFontSize,
  loadFontFamily,
  loadFontSize,
  loadInboxType,
  loadViewMode,
  saveFontFamily,
  saveFontSize,
  saveInboxType,
  saveViewMode,
  type FontFamily,
  type FontSize,
  type InboxType,
  type ViewMode,
} from "./prefs";
import { applyTheme, loadThemePref, saveThemePref, type ThemePref } from "./theme";

const PAGE_SIZE = 50;
const LIST_OVERSCAN = 12;
const LIST_ROW_GROUP_H = 40;
const LIST_ROW_EMAIL_H = 102;
const LIST_ROW_CARD_H = 148;
const LIST_ROW_TABLE_H = 72;

type ListRow =
  | { kind: "group"; key: string; heading: string; count: number }
  | { kind: "email"; email: EmailItem };

const ACCOUNTS_MIN = 180;
const LIST_MIN = 240;
const READER_MIN = 280;
const ACCOUNTS_W_KEY = "email-checker-accounts-w";
const LIST_W_KEY = "email-checker-list-w";
const ACCOUNTS_LIST_H_KEY = "email-checker-accounts-list-h";
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
const EMPTY_FOLDER_COUNTS: Record<MailFolder, number> = {
  inbox: 0,
  sent: 0,
  spam: 0,
  trash: 0,
  archive: 0,
};

function parseFolderCounts(raw: Record<string, number> | undefined): Record<MailFolder, number> {
  const counts = { ...EMPTY_FOLDER_COUNTS };
  for (const key of MAIL_FOLDERS) {
    counts[key] = raw?.[key] ?? 0;
  }
  return counts;
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
  if (mode === "card") return LIST_ROW_CARD_H;
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

export default function App() {
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
  const [folderCounts, setFolderCounts] = useState<Record<MailFolder, number>>(EMPTY_FOLDER_COUNTS);
  const [error, setError] = useState<string | null>(null);
  const [autoSyncStatus, setAutoSyncStatus] = useState<AutoSyncStatus | null>(null);
  const [autoSyncOffline, setAutoSyncOffline] = useState(false);
  const [autoSyncChecking, setAutoSyncChecking] = useState(false);
  const [autoSyncDetailsOpen, setAutoSyncDetailsOpen] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);

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
  const [markAllOpen, setMarkAllOpen] = useState(false);
  const [markingAllRead, setMarkingAllRead] = useState(false);

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [selected, setSelected] = useState<EmailDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [tableDialogOpen, setTableDialogOpen] = useState(false);
  const [collapsedDates, setCollapsedDates] = useState<Set<string>>(new Set());
  const [accountsW, setAccountsW] = useState(() => loadStoredWidth(ACCOUNTS_W_KEY, 260, ACCOUNTS_MIN));
  const [listW, setListW] = useState(() => loadStoredWidth(LIST_W_KEY, 360, LIST_MIN));
  const [accountsListH, setAccountsListH] = useState(() => loadStoredWidth(ACCOUNTS_LIST_H_KEY, 118, 72));
  const [themePref, setThemePref] = useState<ThemePref>(() => loadThemePref());
  const [viewMode, setViewMode] = useState<ViewMode>(() => loadViewMode());
  const [inboxType, setInboxType] = useState<InboxType>(() => loadInboxType());
  const [fontFamily, setFontFamily] = useState<FontFamily>(() => loadFontFamily());
  const [fontSize, setFontSize] = useState<FontSize>(() => loadFontSize());
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("appearance");
  const [resizing, setResizing] = useState(false);
  const [replyOpen, setReplyOpen] = useState(false);
  const [replyText, setReplyText] = useState("");
  const [replyFiles, setReplyFiles] = useState<File[]>([]);
  const [scheduleAt, setScheduleAt] = useState("");
  const [sendingReply, setSendingReply] = useState(false);
  const [aiDrafting, setAiDrafting] = useState(false);
  const [scheduleDialogOpen, setScheduleDialogOpen] = useState(false);
  const [schedulePickerOpen, setSchedulePickerOpen] = useState(false);

  const filterRef = useRef({
    mailboxId: selectedMailboxId,
    label,
    interviewSubtype,
    folder,
    query: searchQuery,
    inboxType,
  });
  filterRef.current = {
    mailboxId: selectedMailboxId,
    label,
    interviewSubtype,
    folder,
    query: searchQuery,
    inboxType,
  };
  const fetchGen = useRef(0);
  const detailGen = useRef(0);
  const loadingMoreRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const readerDialogRef = useRef<HTMLElement>(null);
  const readerCloseRef = useRef<HTMLButtonElement>(null);
  const didMountFilters = useRef(false);
  // Mailbox navigation starts its own load so the list (including date-group
  // counts) is refreshed from the clicked scope immediately.
  const skipNextMailboxFilterLoad = useRef(false);
  const shellRef = useRef<HTMLDivElement>(null);
  const accountsPaneRef = useRef<HTMLElement>(null);
  const dragRef = useRef<{
    which: "accounts" | "list";
    startX: number;
    startAccounts: number;
    startList: number;
  } | null>(null);
  const accountHeightDragRef = useRef<{ startY: number; startH: number } | null>(null);
  const widthsRef = useRef({ accounts: accountsW, list: listW });
  widthsRef.current = { accounts: accountsW, list: listW };
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
    setFolderCounts(parseFolderCounts(page.folder_counts));
  }, []);

  const loadFirstPage = useCallback(async () => {
    const gen = ++fetchGen.current;
    const {
      mailboxId,
      label: currentLabel,
      interviewSubtype: currentSubtype,
      folder: currentFolder,
      query,
      inboxType: currentInboxType,
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
        folder: currentFolder,
        query,
        inboxType: currentInboxType,
        limit: PAGE_SIZE,
      });
      if (gen !== fetchGen.current) return;
      setEmails(sortEmailsForInbox(page.items, currentInboxType));
      applyPageMeta(page);
    } catch (err) {
      if (gen !== fetchGen.current) return;
      setError(err instanceof Error ? err.message : "Failed to load emails");
      setEmails([]);
      setFilteredTotal(0);
    } finally {
      if (gen === fetchGen.current) setLoading(false);
    }
  }, [applyPageMeta]);

  const loadMore = useCallback(async () => {
    if (loadingMoreRef.current || !hasMore || nextCursor == null) return;
    loadingMoreRef.current = true;
    setLoadingMore(true);
    const gen = fetchGen.current;
    const {
      mailboxId,
      label: currentLabel,
      interviewSubtype: currentSubtype,
      folder: currentFolder,
      query,
      inboxType: currentInboxType,
    } = filterRef.current;
    try {
      const page = await fetchEmails({
        mailboxId,
        label: currentLabel,
        interviewSubtype: currentSubtype === "all" ? null : currentSubtype,
        folder: currentFolder,
        query,
        inboxType: currentInboxType,
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
      applyPageMeta(page);
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

  const refreshReclassifyStatuses = useCallback(async () => {
    if (!mailboxes.length) return;
    const results = await Promise.allSettled(mailboxes.map((mailbox) => fetchReclassifyStatus(mailbox.id)));
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
  }, [mailboxes]);

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

  const refreshAutoSyncStatus = useCallback(async () => {
    try {
      const status = await fetchAutoSyncStatus();
      setAutoSyncStatus(status);
      setAutoSyncOffline(false);
    } catch {
      setAutoSyncOffline(true);
    }
  }, []);

  useEffect(() => {
    void refreshAutoSyncStatus();
    const timer = window.setInterval(() => void refreshAutoSyncStatus(), 10_000);
    return () => window.clearInterval(timer);
  }, [refreshAutoSyncStatus]);

  useEffect(() => {
    void refreshReclassifyStatuses();
    const timer = window.setInterval(() => void refreshReclassifyStatuses(), 3_000);
    return () => window.clearInterval(timer);
  }, [refreshReclassifyStatuses]);

  async function retryAutoSync() {
    if (autoSyncChecking) return;
    setAutoSyncChecking(true);
    try {
      const status = autoSyncOffline ? await fetchAutoSyncStatus() : await checkAutoSyncNow();
      setAutoSyncStatus(status);
      setAutoSyncOffline(false);
    } catch {
      setAutoSyncOffline(true);
    } finally {
      setAutoSyncChecking(false);
    }
  }

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
    const source = new EventSource(eventsUrl());
    source.addEventListener("connected", () => {
      void load();
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
        };
        const { mailboxId, label: currentLabel, interviewSubtype: currentSubtype, folder: currentFolder, query } = filterRef.current;
        const matchesMailbox = mailboxId == null || item.mailbox_id === mailboxId;
        const matchesLabel = currentLabel === "all" || item.label === currentLabel;
        const matchesSubtype = currentSubtype === "all" || item.interview_subtype === currentSubtype;
        const itemFolder = mailFolderOf(item);
        const matchesFolder = currentFolder === "all" || itemFolder === currentFolder;
        const needle = query.trim().toLowerCase();
        const matchesQuery =
          !needle ||
          `${item.subject}\n${item.sender}\n${item.snippet}`.toLowerCase().includes(needle);
        const isUpdate = Boolean(item.updated);

        setEmails((prev) => {
          const inbox = filterRef.current.inboxType;
          const existingIdx = prev.findIndex((e) => e.id === item.id);
          if (existingIdx >= 0) {
            if (matchesMailbox && matchesLabel && matchesSubtype && matchesFolder && matchesQuery) {
              const next = [...prev];
              next[existingIdx] = {
                ...next[existingIdx],
                ...item,
                is_read: Boolean(item.is_read ?? next[existingIdx].is_read),
              };
              return sortEmailsForInbox(next, inbox);
            }
            return prev.filter((e) => e.id !== item.id);
          }
          if (!isUpdate && matchesMailbox && matchesLabel && matchesSubtype && matchesFolder && matchesQuery) {
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
          if (matchesMailbox) {
            setLabelCounts((prev) => {
              const next = { ...prev };
              if (item.previous_label && item.previous_label !== item.label) {
                next[item.previous_label] = Math.max(0, (next[item.previous_label] ?? 1) - 1);
              }
              next[item.label] = (next[item.label] ?? 0) + 1;
              return next;
            });
          }
          return;
        }

        if (!item.is_read) {
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
          setFolderCounts((prev) => ({
            ...prev,
            [itemFolder]: (prev[itemFolder] ?? 0) + 1,
          }));
        }
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
        void load();
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
        void load();
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
  }, [load]);

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
      localStorage.setItem(ACCOUNTS_W_KEY, String(Math.round(widthsRef.current.accounts)));
      localStorage.setItem(LIST_W_KEY, String(Math.round(widthsRef.current.list)));
    } catch {
      /* ignore */
    }
  }

  function onSplitterPointerDown(which: "accounts" | "list") {
    return (event: PointerEvent<HTMLButtonElement>) => {
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
      setResizing(true);
      dragRef.current = {
        which,
        startX: event.clientX,
        startAccounts: widthsRef.current.accounts,
        startList: widthsRef.current.list,
      };
    };
  }

  function onAccountHeightSplitterDown(event: PointerEvent<HTMLButtonElement>) {
    event.preventDefault();
    accountHeightDragRef.current = { startY: event.clientY, startH: accountsListH };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function onAccountHeightSplitterMove(event: PointerEvent<HTMLButtonElement>) {
    const drag = accountHeightDragRef.current;
    const pane = accountsPaneRef.current;
    if (!drag || !pane) return;
    const max = Math.max(72, pane.clientHeight - 270);
    setAccountsListH(Math.round(Math.max(72, Math.min(max, drag.startH + event.clientY - drag.startY))));
  }

  function onAccountHeightSplitterUp() {
    accountHeightDragRef.current = null;
    try { localStorage.setItem(ACCOUNTS_LIST_H_KEY, String(Math.round(accountsListH))); } catch { /* ignore */ }
  }

  function onSplitterPointerMove(event: PointerEvent<HTMLButtonElement>) {
    const drag = dragRef.current;
    const shell = shellRef.current;
    if (!drag || !shell) return;
    const width = shell.getBoundingClientRect().width;
    const dx = event.clientX - drag.startX;
    if (drag.which === "accounts") {
      const max = Math.max(ACCOUNTS_MIN, width - drag.startList - READER_MIN - 12);
      setAccountsW(Math.round(Math.min(max, Math.max(ACCOUNTS_MIN, drag.startAccounts + dx))));
    } else {
      const max = Math.max(LIST_MIN, width - drag.startAccounts - READER_MIN - 12);
      setListW(Math.round(Math.min(max, Math.max(LIST_MIN, drag.startList + dx))));
    }
  }

  function onSplitterPointerUp() {
    if (!dragRef.current) return;
    dragRef.current = null;
    setResizing(false);
    persistPaneWidths();
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

  function chooseFontFamily(next: FontFamily) {
    setFontFamily(next);
    saveFontFamily(next);
  }

  function chooseFontSize(next: FontSize) {
    setFontSize(next);
    saveFontSize(next);
  }

  function openSettings(section: SettingsSection = "appearance") {
    setSettingsSection(section);
    setSettingsOpen(true);
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

  const allCount = useMemo(
    () => CLASSIFY_LABELS.reduce((sum, key) => sum + (labelCounts[key] ?? 0), 0),
    [labelCounts]
  );

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
      const unread = emails.filter((email) => !email.is_read);
      const read = emails.filter((email) => email.is_read);
      if (unread.length) {
        rows.push({
          kind: "group",
          key: "phase-unread",
          heading: "Unread",
          count: unread.length,
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
  }, [collapsedDates, dateGroups, emails, inboxType]);

  const listRowsRef = useRef(listRows);
  listRowsRef.current = listRows;
  const viewModeRef = useRef(viewMode);
  viewModeRef.current = viewMode;

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

  const virtualItems = listVirtualizer.getVirtualItems();
  const lastVirtualIndex = virtualItems.length ? virtualItems[virtualItems.length - 1].index : -1;

  useEffect(() => {
    listVirtualizer.measure();
  }, [viewMode, listVirtualizer]);

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

  async function applyCategoryChange(useTraining: boolean) {
    if (!selected || !pendingLabel || savingLabel) return;
    if (pendingLabel.nextLabel === "interview_scheduled" && !pendingSubtype) return;
    const emailId = pendingLabel.emailId;
    const nextLabel = pendingLabel.nextLabel;
    const subtype = nextLabel === "interview_scheduled" ? pendingSubtype as InterviewSubtype : undefined;
    const previous = selected.label;
    const previousSubtype = selected.interview_subtype;
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
    const request = ++detailGen.current;
    setSelectedId(id);
    setSelected(null);
    setSaveTraining(false);
    setPendingLabel(null);
    setLoadingDetail(true);
    try {
      const detail = await fetchEmailDetail(id);
      if (request !== detailGen.current) return;
      setSelected(detail);
      setEmails((prev) => {
        const opened = prev.find((item) => item.id === id);
        if (opened && !opened.is_read) {
          setMailboxUnreadCounts((counts) => ({
            ...counts,
            [opened.mailbox_id]: Math.max(0, (counts[opened.mailbox_id] ?? 1) - 1),
          }));
        }
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
        if (viewMode === "table") setTableDialogOpen(false);
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
  }

  function openTableEmail(id: number) {
    setTableDialogOpen(true);
    void onOpenEmail(id);
  }

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

  function onMessageListKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (viewMode === "table" && event.key === "Enter" && event.target === event.currentTarget && selectedId != null) {
      event.preventDefault();
      openTableEmail(selectedId);
      return;
    }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const target = event.target as HTMLElement;
    if (target.closest("input, textarea, select, [contenteditable='true']")) return;
    const emailRows = listRows
      .map((row, index) => row.kind === "email" ? { id: row.email.id, index } : null)
      .filter((row): row is { id: number; index: number } => row !== null);
    if (!emailRows.length) return;

    event.preventDefault();
    const current = emailRows.findIndex((row) => row.id === selectedId);
    const next = current < 0
      ? event.key === "ArrowDown" ? 0 : emailRows.length - 1
      : Math.max(0, Math.min(emailRows.length - 1, current + (event.key === "ArrowDown" ? 1 : -1)));
    const row = emailRows[next];
    event.currentTarget.focus();
    if (row.id === selectedId) return;
    listVirtualizer.scrollToIndex(row.index, { align: "auto" });
    if (viewMode === "table") selectTableEmail(row.id);
    else void onOpenEmail(row.id);
  }

  function selectMailbox(id: number | null) {
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
        await sendEmail({ mailbox_id: replyMailbox.id, to_address: selected.sender.match(/<([^>]+)>/)?.[1] ?? selected.sender, subject: /^re:/i.test(selected.subject) ? selected.subject : `Re: ${selected.subject}`, body_text: replyText, attachments: await filePayload(replyFiles) });
        setBanner(scheduleAt ? "Reply scheduled and sent." : "Reply sent. It is now in Sent.");
        setReplyOpen(false); setReplyText(""); setReplyFiles([]); setScheduleAt("");
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
    try { setReplyText((await createAiReply(selected.id)).body_text); }
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

  function selectFolder(next: "all" | MailFolder) {
    scrollRef.current?.scrollTo(0, 0);
    if (folder === next) {
      setEmails([]);
      void loadFirstPage();
      return;
    }
    setFolder(next);
  }

  const selectedMailbox = selectedMailboxId != null ? mailboxById.get(selectedMailboxId) : null;
  const replyMailbox = selected ? mailboxById.get(selected.mailbox_id) ?? selectedMailbox : selectedMailbox;
  const scopeUnread = useMemo(() => {
    if (selectedMailboxId == null) {
      return Object.values(mailboxUnreadCounts).reduce((sum, n) => sum + n, 0);
    }
    return mailboxUnreadCounts[selectedMailboxId] ?? 0;
  }, [selectedMailboxId, mailboxUnreadCounts]);

  async function confirmMarkAllRead() {
    const scopeId = selectedMailboxId;
    setMarkingAllRead(true);
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
      setMarkAllOpen(false);
    } catch (err) {
      setBanner(err instanceof Error ? err.message : "Could not mark messages read");
    } finally {
      setMarkingAllRead(false);
    }
  }
  const readerAccount =
    selected != null ? mailboxById.get(selected.mailbox_id)?.email_address ?? "" : "";
  const autoSyncState = autoSyncOffline ? "offline" : autoSyncStatus?.state ?? "checking";
  const autoSyncAlert = ["offline", "no_account", "stopped", "error"].includes(autoSyncState);
  const syncIntervalMinutes = Math.round((autoSyncStatus?.interval_seconds ?? 120) / 60);
  const webhookDeliveryStates = Object.values(autoSyncStatus?.webhook_delivery ?? {});
  const legacyWebhookVerified = webhookDeliveryStates.length === 0
    && Boolean(autoSyncStatus?.connected_accounts)
    && autoSyncStatus?.webhook_accounts === autoSyncStatus?.connected_accounts;
  const webhookDeliveryVerified = legacyWebhookVerified
    || (webhookDeliveryStates.length > 0
      && webhookDeliveryStates.every((delivery) => delivery.healthy));
  const registeredWebhookAccounts = autoSyncStatus?.registered_webhook_accounts
    ?? autoSyncStatus?.webhook_accounts
    ?? 0;
  const autoSyncLabel = autoSyncState === "active"
    ? autoSyncStatus
      && webhookDeliveryVerified
      && autoSyncStatus.webhook_accounts === autoSyncStatus.connected_accounts
      ? "Webhook sync on"
      : webhookDeliveryVerified && autoSyncStatus?.webhook_accounts
        ? `Webhook + auto sync · every ${syncIntervalMinutes} min`
        : registeredWebhookAccounts
          ? `Polling fallback · ${syncIntervalMinutes} min`
          : `Auto sync on · every ${syncIntervalMinutes} min`
    : autoSyncState === "syncing"
      ? "Auto sync running"
      : autoSyncState === "checking"
        ? "Checking auto sync…"
        : "Auto sync off";
  const autoSyncReason = autoSyncState === "no_account"
    ? "Connect an email account to start automatic syncing."
    : autoSyncState === "offline"
      ? "The app cannot reach the email checker server. Start the server, then check again."
      : autoSyncState === "stopped"
        ? "The automatic sync worker is not running. Restart the email checker server, then check again."
        : autoSyncStatus?.problem_mailboxes[0]
          ? `${autoSyncStatus.problem_mailboxes[0].email_address}: ${autoSyncStatus.problem_mailboxes[0].message}`
          : autoSyncStatus?.last_error ?? "An account could not sync. Check the connection and try again.";

  if (settingsOpen) {
    return (
      <SettingsPage
        section={settingsSection}
        onSectionChange={setSettingsSection}
        onClose={() => setSettingsOpen(false)}
        themePref={themePref}
        onThemeChange={chooseTheme}
        viewMode={viewMode}
        onViewModeChange={chooseViewMode}
        inboxType={inboxType}
        onInboxTypeChange={chooseInboxType}
        fontFamily={fontFamily}
        onFontFamilyChange={chooseFontFamily}
        fontSize={fontSize}
        onFontSizeChange={chooseFontSize}
        mailboxes={orderedMailboxes}
        unusedTraining={unusedTraining}
        updatingPrompt={updatingPrompt}
        onUpdatePrompt={onUpdatePrompt}
        onTrainingDeleted={refreshPromptStatus}
        onAddAccount={() => {
          setSettingsOpen(false);
          setConnectEmail("");
          setConnectProvider("google");
          setDialogStep("type");
          setAddAccountOpen(true);
        }}
        onRemoveAccount={onDisconnect}
        syncingIds={syncingIds}
        reclassifyingIds={reclassifyingIds}
        onSyncAccount={(id) => onSync(id, false)}
        onStopSyncAccount={(id) => onStopSync(id, false)}
        onReclassifyAccount={(id) => onReclassify(id, false)}
        onStopReclassifyAccount={(id) => onStopReclassify(id, false)}
        accountStatus={mailboxActions}
        reclassifyProgress={reclassifyProgress}
        initialMailboxId={selectedMailboxId}
      />
    );
  }

  return (
    <div
      ref={shellRef}
      className={`app-shell view-${viewMode}${viewMode === "table" && tableDialogOpen ? " table-dialog-open" : ""}${resizing ? " is-resizing" : ""}`}
      style={
        {
          "--accounts-w": `${accountsW}px`,
          "--list-w": `${listW}px`,
          "--accounts-list-h": `${accountsListH}px`,
        } as CSSProperties
      }
    >
      <aside className="pane-accounts" ref={accountsPaneRef}>
        <div className="accounts-top">
          <div className="brand-mini">Auto AI Email Checker</div>
          <div className="accounts-top-actions">
            <div className="auto-sync-header-wrap auto-sync-top-wrap">
              <div className="auto-sync-mini" data-state={autoSyncState}>
                <button
                  type="button"
                  className="auto-sync-status-button"
                  aria-label={`Email sync status: ${autoSyncLabel}. Show details`}
                  aria-expanded={autoSyncDetailsOpen}
                  aria-controls="auto-sync-details"
                  onClick={() => setAutoSyncDetailsOpen((open) => !open)}
                >
                  <span className="auto-sync-status-icon" aria-hidden="true">
                    {autoSyncAlert ? "!" : autoSyncState === "active" ? "✓" : "↻"}
                  </span>
                  <span role="status" aria-live="polite">{autoSyncLabel}</span>
                </button>
              </div>
              {autoSyncDetailsOpen && autoSyncAlert && (
                <div id="auto-sync-details" className="auto-sync-details auto-sync-header-details">
                  <p>{autoSyncReason}</p>
                  {autoSyncState === "error" && autoSyncStatus && autoSyncStatus.problem_mailboxes.length > 1 && (
                    <p>{autoSyncStatus.problem_mailboxes.length} accounts need attention.</p>
                  )}
                  <div className="auto-sync-actions">
                    {autoSyncState === "no_account" ? (
                      <button type="button" className="action-btn" onClick={() => openSettings("accounts")}>Accounts</button>
                    ) : (
                      <button type="button" className="action-btn" disabled={autoSyncChecking} onClick={() => void retryAutoSync()}>
                        {autoSyncChecking ? "Checking…" : autoSyncState === "error" ? "Retry sync" : "Check again"}
                      </button>
                    )}
                  </div>
                </div>
              )}
            </div>
            <button
              type="button"
              className="settings-btn"
              aria-label="Settings"
              aria-expanded={settingsOpen}
              onClick={() => openSettings("appearance")}
            >
              ⚙
            </button>
          </div>
        </div>
        <button
          type="button"
          className={selectedMailboxId == null ? "nav-inbox active" : "nav-inbox"}
          onClick={() => selectMailbox(null)}
        >
          <span className="nav-inbox-icon" aria-hidden="true">
            ▣
          </span>
          <span>Inbox</span>
          {inboxCount > 0 && <span className="nav-count">{inboxCount}</span>}
        </button>

        <div className="accounts-label">Accounts</div>
        <div className="accounts-scroll">
          {orderedMailboxes.map((box) => {
            const unread = mailboxUnreadCounts[box.id] ?? 0;
            return (
              <div
                key={box.id}
                className={draggedAccountId === box.id ? "account-row-wrap is-dragging" : "account-row-wrap"}
                draggable
                onDragStart={(event) => onAccountDragStart(event, box.id)}
                onDragOver={(event) => event.preventDefault()}
                onDrop={(event) => onAccountDrop(event, box.id)}
                onDragEnd={() => setDraggedAccountId(null)}
              >
                <button
                  type="button"
                  className={
                    selectedMailboxId === box.id ? "account-row active" : "account-row"
                  }
                  onClick={() => selectMailbox(box.id)}
                  title={box.email_address}
                >
                  <span className={`avatar provider-${box.provider}`}>
                    {providerMark(box.provider)}
                  </span>
                  <span className="account-main">
                    <span className="account-email">{box.email_address}</span>
                  </span>
                  {unread > 0 && (
                    <span className="account-new-badge" title={`${unread} new messages`}>
                      {unread}
                    </span>
                  )}
                </button>
              </div>
            );
          })}
          {!mailboxes.length && <p className="hint">Add an account to start automatic sync.</p>}
        </div>
        <button
          type="button"
          className="account-height-splitter"
          aria-label="Resize accounts and filters panels"
          title="Drag to resize account list"
          onPointerDown={onAccountHeightSplitterDown}
          onPointerMove={onAccountHeightSplitterMove}
          onPointerUp={onAccountHeightSplitterUp}
          onPointerCancel={onAccountHeightSplitterUp}
        />

        <div className="account-filter-panel" aria-label="Email filters">
          <div className="account-filter-section">
            <span className="account-filter-title">Folders</span>
            <div className="folder-badges account-filter-list" role="tablist" aria-label="Mail folders">
              <button type="button" className={folder === "all" ? "folder-badge active" : "folder-badge"} onClick={() => selectFolder("all")}>
                <span>All mail</span><span className="count-pill">{allCount}</span>
              </button>
              {MAIL_FOLDERS.map((item) => (
                <button key={item} type="button" className={folder === item ? `folder-badge active folder-${item}` : `folder-badge folder-${item}`} onClick={() => selectFolder(item)}>
                  <span>{MAIL_FOLDER_TITLES[item]}</span><span className="count-pill">{folderCounts[item]}</span>
                </button>
              ))}
            </div>
          </div>
          <div className="account-filter-section">
            <span className="account-filter-title">Categories</span>
            <div className="classify-badges account-filter-list" role="tablist" aria-label="Classifications">
              <button type="button" className={label === "all" ? "classify-badge active" : "classify-badge"} onClick={() => selectLabel("all")}>
                <span>All categories</span><span className="count-pill">{allCount}</span>
              </button>
              {CLASSIFY_LABELS.map((item) => (
                <button key={item} type="button" className={label === item ? `classify-badge active label-${item}` : `classify-badge label-${item}`} onClick={() => selectLabel(item)}>
                  <span>{CLASSIFY_LABEL_TITLES[item]}</span><span className="count-pill">{labelCounts[item]}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      </aside>

      <button
        type="button"
        className="pane-splitter accounts-splitter"
        aria-label="Resize accounts pane"
        onPointerDown={onSplitterPointerDown("accounts")}
        onPointerMove={onSplitterPointerMove}
        onPointerUp={onSplitterPointerUp}
        onPointerCancel={onSplitterPointerUp}
      />

      <section className={`pane-list view-${viewMode}`}>
        <header className="list-header">
          <div className="list-title">
            <div className="list-title-row">
              <h2>
                {selectedMailbox ? selectedMailbox.email_address : "All accounts"}
              </h2>
              <div className="list-header-actions">
                <button
                  type="button"
                  className="action-btn"
                  disabled={scopeUnread === 0 || markingAllRead}
                  onClick={() => setMarkAllOpen(true)}
                >
                  Mark all as read
                </button>
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
            <select
              id="interview-subtype-filter"
              className="subtype-filter-select"
              value={interviewSubtype}
              onChange={(event) => selectInterviewSubtype(event.target.value as "all" | InterviewSubtype)}
            >
              <option value="all">All subtypes</option>
              {(Object.entries(INTERVIEW_SUBTYPE_TITLES) as [InterviewSubtype, string][]).map(([value, title]) => (
                <option key={value} value={value}>{title}</option>
              ))}
            </select>
            {!loading && <span className="subtype-filter-count" role="status">{filteredTotal} email{filteredTotal === 1 ? "" : "s"}</span>}
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

        {viewMode === "table" && <p className="table-view-hint">Double-click an email to open it.</p>}
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
                const accountAddress =
                  selectedMailboxId == null
                    ? mailboxById.get(email.mailbox_id)?.email_address ?? ""
                    : "";
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
                      when={when}
                      folder={mailFolderOf(email)}
                      folderTitle={MAIL_FOLDER_TITLES[mailFolderOf(email)]}
                      onOpen={(id) => viewMode === "table" ? selectTableEmail(id) : void onOpenEmail(id)}
                      onDoubleOpen={openTableEmail}
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
        aria-label="Resize message list"
        onPointerDown={onSplitterPointerDown("list")}
        onPointerMove={onSplitterPointerMove}
        onPointerUp={onSplitterPointerUp}
        onPointerCancel={onSplitterPointerUp}
      />

      {viewMode === "table" && tableDialogOpen && (
        <div className="table-dialog-backdrop" role="presentation" onClick={closeTableDialog} />
      )}
      <section
        className="pane-reader"
        ref={readerDialogRef}
        role={viewMode === "table" && tableDialogOpen ? "dialog" : undefined}
        aria-modal={viewMode === "table" && tableDialogOpen ? true : undefined}
        aria-label={viewMode === "table" && tableDialogOpen ? selected?.subject || "Email" : undefined}
      >
        {viewMode === "table" && tableDialogOpen && (
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
        {loadingDetail && <div className="reader-empty"><p>Opening…</p></div>}
        {selected && !loadingDetail && (
          <article className="reader-article">
            <h1 id="reader-title">{selected.subject || "(no subject)"}</h1>
            <div className="reader-card">
              <div className="reader-meta">
                <div className="reader-people">
                  <span className={`avatar soft label-${selected.label}`}>
                    {initialsFrom(senderName(selected.sender))}
                  </span>
                  <div>
                    <strong>{selected.sender || senderName(selected.sender)}</strong>
                    {readerAccount ? (
                      <p>
                        Account <span className="to-address">{readerAccount}</span>
                      </p>
                    ) : null}
                    <p>
                      Folder{" "}
                      <span className={`folder-pill folder-${mailFolderOf(selected)}`}>
                        {MAIL_FOLDER_TITLES[mailFolderOf(selected)]}
                      </span>
                    </p>
                  </div>
                </div>
                <div className="reader-side">
                  <select
                    className={`label-select label-${selected.label}`}
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
                  </select>
                  {selected.label === "interview_scheduled" && selected.interview_subtype && (
                    <label className="interview-subtype-field">
                      <span>Interview event</span>
                      <span className="interview-subtype-control">
                        <select
                        value={selected.interview_subtype}
                        disabled={savingLabel || pendingLabel != null}
                        aria-label="Interview subtype"
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
                        </select>
                      </span>
                    </label>
                  )}
                  <label className="train-check">
                    <input
                      type="checkbox"
                      checked={saveTraining}
                      onChange={(event) => setSaveTraining(event.target.checked)}
                    />
                    Save as training data
                  </label>
                  <time>{formatReaderTime(selected.received_at)}</time>
                </div>
              </div>
              <EmailBody
                html={selected.body_html}
                text={selected.body_text}
                snippet={selected.snippet}
              />
              {!replyOpen && (
                <div className="reply-action-row">
                  <button type="button" className="action-btn primary" onClick={() => { setScheduleAt(""); setReplyOpen(true); }}>
                    ↩ Reply
                  </button>
                </div>
              )}
              {replyOpen && (
                <section className="reply-composer" aria-label="Reply composer">
                  <header>
                    <strong>Reply</strong>
                    <span>From {replyMailbox?.email_address ?? "selected account"} to {selected.sender}</span>
                    <button type="button" aria-label="Delete reply draft" title="Delete draft" onClick={() => { setReplyOpen(false); setReplyText(""); setReplyFiles([]); setScheduleAt(""); }}>🗑</button>
                  </header>
                  <textarea autoFocus value={replyText} onChange={(event) => setReplyText(event.target.value)} placeholder="Write your reply" />
                  <footer>
                    <div className="reply-tools">
                      <button type="button" className="action-btn" disabled={aiDrafting} onClick={() => void draftAiReply()}>{aiDrafting ? "Drafting…" : "✦ AI draft"}</button>
                      <label className="reply-attach">📎 Attach<input type="file" multiple onChange={(event) => setReplyFiles(Array.from(event.target.files ?? []))} /></label>
                      {replyFiles.length > 0 && <span>{replyFiles.map((file) => file.name).join(", ")}</span>}
                    </div>
                    <div className="reply-send-tools">
                      <div className="send-split"><button type="button" className="primary-btn" disabled={sendingReply || !replyText.trim() || !replyMailbox} onClick={() => { setScheduleAt(""); void sendReply(); }}>{sendingReply ? "Sending…" : "Send"}</button><button type="button" className="primary-btn send-menu" title="Schedule send" aria-label="Schedule send" disabled={sendingReply || !replyMailbox} onClick={() => { setSchedulePickerOpen(false); setScheduleDialogOpen(true); }}>◷</button></div>
                    </div>
                  </footer>
                </section>
              )}
            </div>
          </article>
        )}
      </section>

      {scheduleDialogOpen && (
        <div className="dialog-backdrop" role="presentation">
          <div className="dialog schedule-dialog" role="dialog" aria-modal="true" aria-labelledby="schedule-send-title">
            <header className="dialog-header"><div><h3 id="schedule-send-title">Schedule send</h3><p>Eastern Daylight Time</p></div><button type="button" className="dialog-close" aria-label="Close" onClick={() => setScheduleDialogOpen(false)}>×</button></header>
            <div className="schedule-presets"><button type="button" onClick={() => chooseSchedulePreset(1, 8)}><span>Tomorrow morning</span><small>8:00 AM</small></button><button type="button" onClick={() => chooseSchedulePreset(0, 13)}><span>This afternoon</span><small>1:00 PM</small></button><button type="button" onClick={() => chooseSchedulePreset((8 - new Date().getDay()) % 7 || 7, 8)}><span>Monday morning</span><small>8:00 AM</small></button></div>
            <div className="schedule-custom"><button type="button" onClick={() => setSchedulePickerOpen((open) => !open)}>▣ <span>Pick date & time</span></button>{schedulePickerOpen && <input type="datetime-local" value={scheduleAt} min={new Date().toISOString().slice(0, 16)} onChange={(event) => setScheduleAt(event.target.value)} />}</div>
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
                  Yes
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {pendingLabel && (
        <div className="dialog-backdrop" role="presentation">
          <div
            className="dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="train-confirm-title"
          >
            <header className="dialog-header">
              <h3 id="train-confirm-title">Prompt training</h3>
            </header>
            <div className="dialog-body">
              {pendingLabel.nextLabel === "interview_scheduled" && (
                <label className="dialog-email-label">
                  Interview event
                  <select
                    value={pendingSubtype}
                    onChange={(event) => setPendingSubtype(event.target.value as InterviewSubtype | "")}
                    aria-label="Interview subtype"
                    required
                  >
                    <option value="">Choose a subtype</option>
                    {(Object.entries(INTERVIEW_SUBTYPE_TITLES) as [InterviewSubtype, string][]).map(([value, title]) => (
                      <option key={value} value={value}>{title}</option>
                    ))}
                  </select>
                </label>
              )}
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
