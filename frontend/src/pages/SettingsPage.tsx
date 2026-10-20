import { useEffect, useState, type CSSProperties } from "react";
import {
  deleteClassifyTrainingExample,
  deleteUnusedClassifyTraining,
  fetchClassifyPromptStatus,
  fetchClassifyTraining,
  fetchAiSettings,
  fetchGeminiModels,
  fetchOllamaModels,
  fetchOpenAiCosts,
  saveAiSettings,
  type AccountSyncDetail,
  type AiSettings,
  type OpenAiCosts,
  type ClassifyPromptStatus,
  type ClassifyTrainingExample,
  type Mailbox,
  type SessionUser,
} from "../api";
import { accountColor } from "../accountColor";
import { CLASSIFY_LABEL_TITLES, INTERVIEW_SUBTYPE_TITLES } from "../labels";
import {
  FONT_FAMILIES,
  FONT_SIZES,
  INBOX_TYPES,
  READING_PANES,
  VIEW_MODES,
  type FontFamily,
  type FontSize,
  type InboxType,
  type ReadingPane,
  type ViewMode,
} from "../prefs";
import type { ThemePref } from "../theme";
import CompactSelect from "../components/CompactSelect";
import { IconButton } from "../components/UiIcon";
import SignInAccountsPanel from "../components/SignInAccountsPanel";

export type SettingsSection = "signin" | "accounts" | "appearance" | "ai" | "training";

const ADMIN_SETTINGS_SECTIONS = new Set<SettingsSection>(["ai", "training"]);
const TRAINING_PAGE_SIZE = 20;
const DEFAULT_GEMINI_MODELS = [
  "gemini-3.8-flash",
  "gemini-3.5-flash-lite",
  "gemini-2.5-flash",
  "gemini-2.5-flash-lite",
  "gemini-2.5-pro",
];

function formatWhen(value: string | null): string {
  if (!value) return "";
  const hasZone = /Z$/i.test(value) || /[+-]\d{2}:\d{2}$/.test(value);
  const d = new Date(hasZone ? value : `${value}Z`);
  if (Number.isNaN(d.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(d);
}

function titleForLabel(slug: string): string {
  return CLASSIFY_LABEL_TITLES[slug as keyof typeof CLASSIFY_LABEL_TITLES] ?? slug;
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

function timeAgo(value: string | null | undefined): string | null {
  const time = value ? Date.parse(value) : Number.NaN;
  if (!Number.isFinite(time)) return null;
  const seconds = Math.max(0, Math.floor((Date.now() - time) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

const ACCOUNT_ICONS = {
  sync: <><path d="M20 7v5h-5M4 17v-5h5" /><path d="M6.1 8.2A7 7 0 0 1 18.8 7L20 12M4 12l1.2 5A7 7 0 0 0 17.9 15.8" /></>,
  stop: <rect x="7" y="7" width="10" height="10" rx="2" />,
  reclassify: <><path d="M11 3.5l1.7 4.4 4.4 1.7-4.4 1.7L11 15.7l-1.7-4.4-4.4-1.7 4.4-1.7z" /><path d="M18 14.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z" /></>,
  reconnect: <><path d="M10 14a4.5 4.5 0 0 0 6.4 0l3.2-3.2a4.5 4.5 0 0 0-6.4-6.4l-1.1 1.1" /><path d="M14 10a4.5 4.5 0 0 0-6.4 0l-3.2 3.2a4.5 4.5 0 0 0 6.4 6.4l1.1-1.1" /></>,
  remove: <><path d="M4 7h16M10 11v6M14 11v6" /><path d="M6 7l1 13h10l1-13M9 7V4h6v3" /></>,
} as const;

function AccountIcon({ name }: { name: keyof typeof ACCOUNT_ICONS }) {
  return <svg viewBox="0 0 24 24" aria-hidden="true">{ACCOUNT_ICONS[name]}</svg>;
}

export default function SettingsPage({
  section,
  onSectionChange,
  onClose,
  themePref,
  onThemeChange,
  viewMode,
  onViewModeChange,
  inboxType,
  onInboxTypeChange,
  readingPane,
  onReadingPaneChange,
  conversationView,
  onConversationViewChange,
  fontFamily,
  onFontFamilyChange,
  fontSize,
  onFontSizeChange,
  mailboxes,
  unusedTraining,
  updatingPrompt,
  onUpdatePrompt,
  onTrainingDeleted,
  onAiSettingsSaved,
  onAddAccount,
  onAccountColor,
  onRemoveAccount,
  onReconnectAccount,
  accountSyncDetails,
  syncingIds,
  reclassifyingIds,
  onSyncAccount,
  onStopSyncAccount,
  onReclassifyAccount,
  onStopReclassifyAccount,
  accountStatus,
  reclassifyProgress,
  sessionUser,
  onSignOut,
}: {
  section: SettingsSection;
  onSectionChange: (section: SettingsSection) => void;
  onClose: () => void;
  themePref: ThemePref;
  onThemeChange: (pref: ThemePref) => void;
  viewMode: ViewMode;
  onViewModeChange: (mode: ViewMode) => void;
  inboxType: InboxType;
  onInboxTypeChange: (type: InboxType) => void;
  readingPane: ReadingPane;
  onReadingPaneChange: (pane: ReadingPane) => void;
  conversationView: boolean;
  onConversationViewChange: (enabled: boolean) => void;
  fontFamily: FontFamily;
  onFontFamilyChange: (font: FontFamily) => void;
  fontSize: FontSize;
  onFontSizeChange: (size: FontSize) => void;
  mailboxes: Mailbox[];
  unusedTraining: number;
  updatingPrompt: boolean;
  onUpdatePrompt: () => Promise<void> | void;
  onTrainingDeleted: () => Promise<void>;
  onAiSettingsSaved: () => Promise<void> | void;
  onAddAccount: () => void;
  onAccountColor: (mailboxId: number, calendarColor: string) => void;
  onRemoveAccount: (mailboxId: number) => Promise<void>;
  onReconnectAccount: (mailboxId: number) => void;
  accountSyncDetails: Record<number, AccountSyncDetail>;
  syncingIds: Set<number>;
  reclassifyingIds: Set<number>;
  onSyncAccount: (mailboxId: number) => Promise<void>;
  onStopSyncAccount: (mailboxId: number) => Promise<void>;
  onReclassifyAccount: (mailboxId: number) => Promise<void>;
  onStopReclassifyAccount: (mailboxId: number) => Promise<void>;
  accountStatus: Record<number, { type: "sync" | "reclassify" | "success" | "error"; message: string }>;
  reclassifyProgress: Record<number, { processed: number; total: number; failed: number }>;
  sessionUser: SessionUser;
  onSignOut: () => void;
}) {
  const [promptStatus, setPromptStatus] = useState<ClassifyPromptStatus | null>(null);
  const [training, setTraining] = useState<ClassifyTrainingExample[]>([]);
  const [trainingTotal, setTrainingTotal] = useState(0);
  const [trainingPage, setTrainingPage] = useState(0);
  const [trainingError, setTrainingError] = useState<string | null>(null);
  const [trainingLoading, setTrainingLoading] = useState(section === "training");
  const [trainingRevision, setTrainingRevision] = useState(0);
  const [deletingTrainingId, setDeletingTrainingId] = useState<number | null>(null);
  const [deletingUnused, setDeletingUnused] = useState(false);
  const [trainingNotice, setTrainingNotice] = useState<string | null>(null);
  const [aiSettings, setAiSettings] = useState<AiSettings | null>(null);
  const [openaiKey, setOpenaiKey] = useState("");
  const [openaiAdminKey, setOpenaiAdminKey] = useState("");
  const [geminiKey, setGeminiKey] = useState("");
  const [showGeminiKey, setShowGeminiKey] = useState(false);
  const [openaiCosts, setOpenaiCosts] = useState<OpenAiCosts | null>(null);
  const [geminiModels, setGeminiModels] = useState<string[]>([]);
  const [ollamaModels, setOllamaModels] = useState<string[]>([]);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiNotice, setAiNotice] = useState<string | null>(null);
  const [removingMailboxId, setRemovingMailboxId] = useState<number | null>(null);

  useEffect(() => {
    void fetchAiSettings().then((settings) => {
      setAiSettings(settings);
      if (settings.openai_admin_key_configured) {
        void fetchOpenAiCosts().then(setOpenaiCosts).catch((err) => setAiNotice(err instanceof Error ? err.message : "Failed to load OpenAI costs"));
      }
    }).catch((err) => setAiNotice(err instanceof Error ? err.message : "Failed to load AI settings"));
  }, []);

  useEffect(() => {
    if (!aiSettings || aiSettings.provider !== "ollama") return;
    void fetchOllamaModels(aiSettings.ollama_url).then(setOllamaModels).catch(() => setOllamaModels([]));
  }, [aiSettings?.provider, aiSettings?.ollama_url]);

  useEffect(() => {
    if (!aiSettings || aiSettings.provider !== "gemini" || !aiSettings.gemini_key_configured) return;
    void fetchGeminiModels().then(setGeminiModels).catch(() => setGeminiModels([]));
  }, [aiSettings?.provider, aiSettings?.gemini_key_configured]);

  async function saveModelSettings() {
    if (!aiSettings || aiBusy) return;
    setAiBusy(true);
    setAiNotice(null);
    try {
      const saved = await saveAiSettings({
        provider: aiSettings.provider,
        openai_model: aiSettings.openai_model,
        openai_api_key: openaiKey || undefined,
        openai_admin_key: openaiAdminKey || undefined,
        gemini_model: aiSettings.gemini_model,
        gemini_api_key: geminiKey || undefined,
        ollama_url: aiSettings.ollama_url,
        ollama_model: aiSettings.ollama_model,
      });
      setAiSettings(saved);
      setOpenaiKey("");
      setOpenaiAdminKey("");
      if (saved.provider !== "gemini") {
        setGeminiKey("");
        setShowGeminiKey(false);
      }
      setAiNotice("AI model settings saved.");
      await onAiSettingsSaved();
      if (saved.openai_admin_key_configured) {
        try {
          setOpenaiCosts(await fetchOpenAiCosts());
        } catch (err) {
          setAiNotice(err instanceof Error ? `Settings saved. ${err.message}` : "Settings saved, but costs could not be loaded.");
        }
      }
    } catch (err) {
      setAiNotice(err instanceof Error ? err.message : "Failed to save AI settings");
    } finally {
      setAiBusy(false);
    }
  }

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (document.querySelector("[aria-expanded='true']")) return;
      event.preventDefault();
      onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  useEffect(() => {
    if (section !== "training") return;
    let cancelled = false;
    setTrainingLoading(true);
    setTrainingError(null);
    Promise.all([
      fetchClassifyTraining({ limit: TRAINING_PAGE_SIZE, offset: trainingPage * TRAINING_PAGE_SIZE }),
      fetchClassifyPromptStatus(),
    ])
      .then(([page, status]) => {
        if (cancelled) return;
        setPromptStatus(status);
        setTrainingTotal(page.total);
        const lastPage = Math.max(0, Math.ceil(page.total / TRAINING_PAGE_SIZE) - 1);
        if (trainingPage > lastPage) {
          setTrainingPage(lastPage);
          return;
        }
        setTraining(page.items);
      })
      .catch((err) => {
        if (!cancelled) {
          setTrainingError(err instanceof Error ? err.message : "Failed to load training data");
        }
      })
      .finally(() => {
        if (!cancelled) setTrainingLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [section, unusedTraining, trainingPage, trainingRevision]);

  async function deleteTrainingExample(id: number) {
    setDeletingTrainingId(id);
    setTrainingError(null);
    setTrainingNotice(null);
    try {
      await deleteClassifyTrainingExample(id);
      setTrainingNotice("Unused training example deleted.");
      setTrainingRevision((revision) => revision + 1);
      await onTrainingDeleted();
    } catch (err) {
      setTrainingError(err instanceof Error ? err.message : "Failed to delete training example");
    } finally {
      setDeletingTrainingId(null);
    }
  }

  async function deleteAllUnusedTraining() {
    if (!window.confirm(`Delete all ${unusedTraining} unused training examples? This cannot be undone.`)) return;
    setDeletingUnused(true);
    setTrainingError(null);
    setTrainingNotice(null);
    try {
      const result = await deleteUnusedClassifyTraining();
      setTrainingNotice(`Deleted ${result.deleted} unused training example${result.deleted === 1 ? "" : "s"}.`);
      setTrainingPage(0);
      setTrainingRevision((revision) => revision + 1);
      await onTrainingDeleted();
    } catch (err) {
      setTrainingError(err instanceof Error ? err.message : "Failed to delete unused training examples");
    } finally {
      setDeletingUnused(false);
    }
  }

  async function handleUpdatePrompt() {
    await onUpdatePrompt();
  }

  const navItems: { id: SettingsSection; label: string }[] = [
    { id: "signin", label: "Sign-in" },
    { id: "accounts", label: "Email accounts" },
    { id: "appearance", label: "Appearance" },
    ...(sessionUser.is_admin
      ? ([
          { id: "ai", label: "AI model" },
          { id: "training", label: "Training" },
        ] as const)
      : []),
  ];

  useEffect(() => {
    if (!sessionUser.is_admin && ADMIN_SETTINGS_SECTIONS.has(section)) {
      onSectionChange("appearance");
    }
  }, [sessionUser.is_admin, section, onSectionChange]);

  return (
    <main className="settings-page" aria-labelledby="settings-title">
      <div className="settings-shell">
        <header className="settings-top">
          <h2 id="settings-title">Settings</h2>
          <IconButton name="back" label="Back to inbox" onClick={onClose} />
        </header>
        <div className="settings-body">
          <nav className="settings-nav" aria-label="Settings sections">
            {navItems.map((item) => (
              <button
                key={item.id}
                type="button"
                className={section === item.id ? "settings-nav-btn active" : "settings-nav-btn"}
                onClick={() => {
                  if (item.id === "training") setTrainingLoading(true);
                  onSectionChange(item.id);
                }}
              >
                {item.label}
                {item.id === "training" && unusedTraining > 0 ? (
                  <span className="count-pill">{unusedTraining}</span>
                ) : null}
              </button>
            ))}
          </nav>
          <div className="settings-content">
            {section === "signin" && <SignInAccountsPanel sessionUser={sessionUser} onSignOut={onSignOut} />}
            {section === "accounts" && (
              <div className="settings-panel">
                <section className="settings-block">
                  <div className="settings-block-heading">
                    <div>
                      <h3>Email accounts</h3>
                      <p className="settings-help">Add connected inboxes, reconnect an account whose sign-in expired, remove accounts, or choose the color used for that account’s interviews on the calendar.</p>
                    </div>
                    <button type="button" className="action-btn primary" onClick={onAddAccount}>+ Add an account</button>
                  </div>
                  <div className="settings-account-list">
                    {mailboxes.map((mailbox) => {
                      const isWorking = syncingIds.has(mailbox.id) || reclassifyingIds.has(mailbox.id);
                      const activeStatus = accountStatus[mailbox.id];
                      const detail = accountSyncDetails[mailbox.id];
                      const needsReconnect = Boolean(detail?.needs_reconnect);
                      const syncing = syncingIds.has(mailbox.id);
                      const reclassifying = reclassifyingIds.has(mailbox.id);
                      const removing = removingMailboxId === mailbox.id;
                      const color = accountColor(mailbox);
                      const lastSync = timeAgo(detail?.last_sync_completed_at);
                      const health = needsReconnect
                        ? { tone: "error", label: "Sign-in expired" }
                        : syncing
                          ? { tone: "busy", label: "Syncing…" }
                          : reclassifying
                            ? { tone: "busy", label: "Reclassifying…" }
                            : detail?.last_error
                              ? { tone: "warn", label: "Sync problem" }
                              : { tone: "ok", label: lastSync ? `Synced ${lastSync}` : "Connected" };
                      const method = detail?.sync_method === "polling" ? "Polling" : detail?.sync_method === "webhook" ? "Live" : null;
                      const statusTooltip = needsReconnect
                        ? "Google stopped accepting this account's saved sign-in. Reconnect to resume syncing."
                        : detail?.last_error ?? (detail?.polling_reason
                          ? `Checked every 2 minutes: ${detail.polling_reason}.`
                          : "New mail arrives instantly through the webhook.");
                      return (
                      <div className={`settings-account-row${needsReconnect ? " needs-attention" : ""}`} key={mailbox.id}>
                        <label
                          className="account-avatar"
                          style={{ "--account-color": color } as CSSProperties}
                          data-tooltip="Change calendar color"
                        >
                          {initialsFrom(mailbox.display_name || mailbox.email_address)}
                          <input
                            type="color"
                            aria-label={`Calendar color for ${mailbox.email_address}`}
                            value={color}
                            onChange={(event) => onAccountColor(mailbox.id, event.target.value)}
                          />
                        </label>
                        <div className="settings-account-info">
                          <strong title={mailbox.email_address}>{mailbox.email_address}</strong>
                          <div className="account-meta">
                            <span className="account-provider">{mailbox.provider === "google" ? "Gmail" : "Outlook"}</span>
                            <span className={`account-health ${health.tone}`} data-tooltip={statusTooltip}>{health.label}</span>
                            {method && !needsReconnect && <span className="account-method">{method}</span>}
                          </div>
                        </div>
                        <div className="settings-account-actions">
                          <button
                            type="button"
                            className={`account-icon-btn${syncing ? " is-active" : ""}`}
                            aria-label={syncing ? "Stop sync" : "Sync now"}
                            data-tooltip={syncing ? "Stop sync" : "Sync now"}
                            disabled={reclassifying || removingMailboxId != null || needsReconnect}
                            onClick={() => void (syncing ? onStopSyncAccount(mailbox.id) : onSyncAccount(mailbox.id))}
                          >
                            <AccountIcon name={syncing ? "stop" : "sync"} />
                          </button>
                          <button
                            type="button"
                            className={`account-icon-btn${reclassifying ? " is-active" : ""}`}
                            aria-label={reclassifying ? "Stop reclassifying" : "Reclassify with AI"}
                            data-tooltip={reclassifying ? "Stop reclassifying" : "Reclassify all emails with AI"}
                            disabled={syncing || removingMailboxId != null}
                            onClick={() => void (reclassifying ? onStopReclassifyAccount(mailbox.id) : onReclassifyAccount(mailbox.id))}
                          >
                            <AccountIcon name={reclassifying ? "stop" : "reclassify"} />
                          </button>
                          <button
                            type="button"
                            className={`account-icon-btn${needsReconnect ? " is-urgent" : ""}`}
                            aria-label="Reconnect"
                            data-tooltip="Reconnect: sign in again (your emails are kept)"
                            disabled={removingMailboxId != null}
                            onClick={() => onReconnectAccount(mailbox.id)}
                          >
                            <AccountIcon name="reconnect" />
                          </button>
                          <span className="account-actions-divider" aria-hidden="true" />
                          <button
                            type="button"
                            className="account-icon-btn danger"
                            aria-label="Remove account"
                            data-tooltip={removing ? "Removing…" : "Remove account and its stored emails"}
                            disabled={syncing || reclassifying || removingMailboxId != null}
                            onClick={() => {
                              if (!window.confirm(`Remove ${mailbox.email_address}? Its stored emails will be deleted from this app.`)) return;
                              setRemovingMailboxId(mailbox.id);
                              void onRemoveAccount(mailbox.id).finally(() => setRemovingMailboxId(null));
                            }}
                          >
                            <AccountIcon name="remove" />
                          </button>
                        </div>
                        {needsReconnect && !isWorking && (
                          <div className="account-alert" role="alert">
                            <span>Sign-in expired — this account isn’t syncing.</span>
                            <button type="button" onClick={() => onReconnectAccount(mailbox.id)}>Reconnect now</button>
                          </div>
                        )}
                        {isWorking && (
                          <p className={`settings-account-status ${activeStatus?.type ?? "sync"}`} role="status">
                            {activeStatus?.message ?? (syncingIds.has(mailbox.id) ? "Syncing this account…" : "Reclassifying this account…")}
                          </p>
                        )}
                        {reclassifyingIds.has(mailbox.id) && (() => {
                          const progress = reclassifyProgress[mailbox.id] ?? { processed: 0, total: 0, failed: 0 };
                          const completed = Math.max(0, progress.processed - progress.failed);
                          const percent = progress.total ? Math.min(100, Math.round(completed * 100 / progress.total)) : 0;
                          return (
                            <div className="settings-account-progress" role="status">
                              <div className="settings-account-progress-label">
                                <span>{completed.toLocaleString()} / {progress.total.toLocaleString()} classified</span>
                                <strong>{percent}%</strong>
                              </div>
                              <div className="modern-progress"><span style={{ width: `${percent}%` }} /></div>
                            </div>
                          );
                        })()}
                      </div>
                    );
                    })}
                    {!mailboxes.length && <p className="settings-help">No email accounts are connected.</p>}
                  </div>
                </section>
              </div>
            )}

            {section === "appearance" && (
              <div className="settings-panel">
                <section className="settings-block">
                  <h3>View mode</h3>
                  <p className="settings-help">How the inbox message list is shown.</p>
                  <div className="appearance-options" role="radiogroup" aria-label="View mode">
                    {VIEW_MODES.map((mode) => (
                      <label key={mode} className={viewMode === mode ? "appearance-row is-selected" : "appearance-row"}>
                        <input
                          type="radio"
                          name="view-mode"
                          checked={viewMode === mode}
                          onChange={() => onViewModeChange(mode)}
                        />
                        <span>{mode === "list" ? "List" : "Table"}</span>
                        <span className={`appearance-preview preview-${mode}`} aria-hidden="true" />
                      </label>
                    ))}
                  </div>
                </section>
                <section className="settings-block">
                  <h3>Reading pane</h3>
                  <p className="settings-help">Where the selected message appears beside the inbox.</p>
                  <div className="appearance-options" role="radiogroup" aria-label="Reading pane">
                    {READING_PANES.map((pane) => (
                      <label key={pane.id} className={readingPane === pane.id ? "appearance-row is-selected" : "appearance-row"}>
                        <input
                          type="radio"
                          name="reading-pane"
                          checked={readingPane === pane.id}
                          onChange={() => onReadingPaneChange(pane.id)}
                        />
                        <span>{pane.label}</span>
                        <span className={`appearance-preview preview-pane-${pane.id.replace("_", "-")}`} aria-hidden="true" />
                      </label>
                    ))}
                  </div>
                </section>
                <section className="settings-block">
                  <h3>Inbox type</h3>
                  <p className="settings-help">Order of messages in the inbox list.</p>
                  <div className="appearance-options" role="radiogroup" aria-label="Inbox type">
                    {INBOX_TYPES.map((type) => (
                      <label key={type} className={inboxType === type ? "appearance-row is-selected" : "appearance-row"}>
                        <input
                          type="radio"
                          name="inbox-type"
                          checked={inboxType === type}
                          onChange={() => onInboxTypeChange(type)}
                        />
                        <span>{type === "default" ? "Default" : "Unread first"}</span>
                        <span className={`appearance-preview preview-inbox-${type === "default" ? "default" : "unread"}`} aria-hidden="true" />
                      </label>
                    ))}
                  </div>
                </section>
                <section className="settings-block">
                  <h3>Email threading</h3>
                  <p className="settings-help">Group messages from the same conversation into one inbox row.</p>
                  <label className={conversationView ? "appearance-row is-selected" : "appearance-row"}>
                    <input
                      type="checkbox"
                      checked={conversationView}
                      onChange={(event) => onConversationViewChange(event.target.checked)}
                    />
                    <span>Conversation view</span>
                    <span className="appearance-preview preview-thread" aria-hidden="true" />
                  </label>
                </section>
                <section className="settings-block">
                  <h3>Theme</h3>
                  <p className="settings-help">Color scheme for the app.</p>
                  <div className="appearance-options" role="radiogroup" aria-label="Theme">
                    {(["system", "dark", "light"] as ThemePref[]).map((item) => (
                      <label key={item} className={themePref === item ? "appearance-row is-selected" : "appearance-row"}>
                        <input
                          type="radio"
                          name="theme"
                          checked={themePref === item}
                          onChange={() => onThemeChange(item)}
                        />
                        <span>{item === "system" ? "System" : item === "dark" ? "Dark" : "Light"}</span>
                        <span className={`appearance-preview preview-theme-${item}`} aria-hidden="true" />
                      </label>
                    ))}
                  </div>
                </section>
                <section className="settings-block">
                  <h3>Font</h3>
                  <p className="settings-help">Typeface and size used across the app.</p>
                  <label className="settings-field">
                    Family
                    <CompactSelect
                      value={fontFamily}
                      onChange={(event) => onFontFamilyChange(event.target.value as FontFamily)}
                    >
                      {FONT_FAMILIES.map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.label}
                        </option>
                      ))}
                    </CompactSelect>
                  </label>
                  <div className="seg-control" role="radiogroup" aria-label="Font size">
                    {FONT_SIZES.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        role="radio"
                        aria-checked={fontSize === item.id}
                        className={fontSize === item.id ? "seg-btn active" : "seg-btn"}
                        onClick={() => onFontSizeChange(item.id)}
                      >
                        {item.label}
                      </button>
                    ))}
                  </div>
                </section>
              </div>
            )}

            {section === "ai" && (
              <div className="settings-panel">
                <section className="settings-block ai-model-settings">
                  <h3>Classification model</h3>
                  <p className="settings-help">Choose the service used by Reclassify, prompt updates, and company or role extraction.</p>
                  {aiSettings ? (
                    <>
                      <div className="model-choice" role="radiogroup" aria-label="AI provider">
                        {(["openai", "gemini", "ollama"] as const).map((provider) => (
                          <button key={provider} type="button" role="radio"
                            aria-checked={aiSettings.provider === provider}
                            className={aiSettings.provider === provider ? "model-card active" : "model-card"}
                            onClick={() => setAiSettings({ ...aiSettings, provider })}>
                            <strong>{provider === "openai" ? "OpenAI" : provider === "gemini" ? "Google Gemini" : "Local AI (Ollama)"}</strong>
                            <span>{provider === "ollama" ? "Private model on your local server" : "Cloud model using your API key"}</span>
                          </button>
                        ))}
                      </div>
                      {aiSettings.provider === "openai" ? (
                        <div className="ai-fields">
                          <label className="settings-field">OpenAI model
                            <input value={aiSettings.openai_model} onChange={(e) => setAiSettings({ ...aiSettings, openai_model: e.target.value })} />
                          </label>
                          <label className="settings-field">OpenAI API key
                            <input type="password" autoComplete="new-password" value={openaiKey}
                              placeholder={aiSettings.openai_key_configured ? "Key is saved — enter a new key to replace it" : "sk-..."}
                              onChange={(e) => setOpenaiKey(e.target.value)} />
                          </label>
                          <label className="settings-field">Organization admin key
                            <input type="password" autoComplete="new-password" value={openaiAdminKey}
                              placeholder={aiSettings.openai_admin_key_configured ? "Admin key is saved — enter a new key to replace it" : "sk-admin-..."}
                              onChange={(e) => setOpenaiAdminKey(e.target.value)} />
                          </label>
                          <div className="balance-card">
                            <strong>OpenAI billing</strong>
                            {openaiCosts
                              ? <span>Organization spend this month: <strong>${openaiCosts.month_spend_usd.toFixed(2)}</strong></span>
                              : <span>Save an organization admin key to show this month&apos;s organization spend.</span>}
                            <span>OpenAI does not provide a documented remaining credit-balance endpoint.</span>
                            {aiSettings.openai_admin_key_configured && (
                              <button type="button" className="action-btn" onClick={() => void fetchOpenAiCosts().then(setOpenaiCosts).catch((err) => setAiNotice(err instanceof Error ? err.message : "Failed to load OpenAI costs"))}>
                                Refresh spending
                              </button>
                            )}
                            <a href={aiSettings.billing_url} target="_blank" rel="noreferrer">Open billing dashboard</a>
                          </div>
                        </div>
                      ) : aiSettings.provider === "gemini" ? (
                        <div className="ai-fields">
                          <label className="settings-field">Gemini model
                            <CompactSelect value={aiSettings.gemini_model} onChange={(e) => setAiSettings({ ...aiSettings, gemini_model: e.target.value })}>
                              {![...DEFAULT_GEMINI_MODELS, ...geminiModels].includes(aiSettings.gemini_model) && (
                                <option value={aiSettings.gemini_model}>{aiSettings.gemini_model}</option>
                              )}
                              {(geminiModels.length ? geminiModels : DEFAULT_GEMINI_MODELS).map((model) => (
                                <option key={model} value={model}>
                                  {model}{model === "gemini-3.8-flash" ? " - Recommended" : ""}
                                </option>
                              ))}
                            </CompactSelect>
                          </label>
                          <label className="settings-field">Gemini API key
                            <span className="secret-input">
                              <input type={showGeminiKey ? "text" : "password"} autoComplete="new-password" value={geminiKey}
                                placeholder={aiSettings.gemini_key_configured ? "Key is saved - enter a new key to replace it" : "Google AI Studio API key"}
                                onChange={(e) => setGeminiKey(e.target.value)} />
                              <button type="button" disabled={!geminiKey} aria-pressed={showGeminiKey}
                                aria-label={showGeminiKey ? "Hide Gemini API key" : "Show Gemini API key"}
                                onClick={() => setShowGeminiKey((shown) => !shown)}>
                                {showGeminiKey ? "Hide" : "Show"}
                              </button>
                            </span>
                          </label>
                          <p className="settings-help">
                            Email content is sent to the Gemini API when this provider is selected.
                          </p>
                        </div>
                      ) : (
                        <div className="ai-fields">
                          <label className="settings-field">Ollama server
                            <input value={aiSettings.ollama_url} onChange={(e) => setAiSettings({ ...aiSettings, ollama_url: e.target.value })} />
                          </label>
                          <label className="settings-field">Ollama model
                            <CompactSelect value={aiSettings.ollama_model} onChange={(e) => setAiSettings({ ...aiSettings, ollama_model: e.target.value })}>
                              {!ollamaModels.includes(aiSettings.ollama_model) && <option value={aiSettings.ollama_model}>{aiSettings.ollama_model}</option>}
                              {ollamaModels.map((model) => (
                                <option key={model} value={model}>
                                  {model}{model === "qwen2.5:14b-instruct" ? " — Recommended" : ""}
                                </option>
                              ))}
                            </CompactSelect>
                          </label>
                          <p className="settings-help">
                            Recommended: qwen2.5:14b-instruct for accurate JSON classification with moderate resource use.
                            Gitea: 192.168.2.230:5000 · Ollama: 192.168.2.230:11440
                          </p>
                        </div>
                      )}
                      <button type="button" className="action-btn primary" disabled={aiBusy} onClick={() => void saveModelSettings()}>
                        {aiBusy ? "Saving…" : "Save AI settings"}
                      </button>
                      {aiNotice && <p className="settings-help" role="status">{aiNotice}</p>}
                    </>
                  ) : <p className="settings-help">Loading AI settings…</p>}
                </section>
              </div>
            )}

            {section === "training" && (
              <div className="settings-panel">
                <section className="settings-block">
                  <h3>Classification training</h3>
                  <p className="settings-help">
                    Category and interview subtype corrections saved from the reader. Unused examples can update the classify prompt.
                  </p>
                  <div className="settings-status-row">
                    <p>
                      {unusedTraining > 0
                        ? `${unusedTraining} unused example${unusedTraining === 1 ? "" : "s"} ready`
                        : "No unused training examples"}
                      {trainingTotal > 0 ? ` · ${trainingTotal} total` : ""}
                    </p>
                    {promptStatus?.updated_at ? (
                      <p className="settings-help">
                        Prompt v{promptStatus.active_version_id ?? "—"}
                        {promptStatus.active_source ? ` · ${promptStatus.active_source}` : ""}
                        {` · updated ${formatWhen(promptStatus.updated_at)}`}
                      </p>
                    ) : null}
                    <div className="training-actions">
                      <button
                        type="button"
                        className={updatingPrompt ? "action-btn busy" : "action-btn"}
                        disabled={updatingPrompt || deletingUnused || deletingTrainingId != null || unusedTraining <= 0}
                        onClick={() => void handleUpdatePrompt()}
                      >
                        {updatingPrompt ? (
                          <>
                            <span className="spinner" aria-hidden="true" />
                            Updating prompt
                          </>
                        ) : unusedTraining > 0 ? (
                          `Update prompt (${unusedTraining})`
                        ) : (
                          "Update prompt"
                        )}
                      </button>
                      <button
                        type="button"
                        className="action-btn danger"
                        disabled={updatingPrompt || deletingUnused || deletingTrainingId != null || unusedTraining <= 0}
                        onClick={() => void deleteAllUnusedTraining()}
                      >
                        {deletingUnused ? "Deleting..." : "Delete all unused"}
                      </button>
                    </div>
                  </div>
                  {trainingError && <p className="error">{trainingError}</p>}
                  {trainingNotice && <p className="hint" role="status">{trainingNotice}</p>}
                  {trainingLoading && <p className="hint">Loading training data…</p>}
                  {!trainingLoading && training.length === 0 && !trainingError && (
                    <p className="hint">Correct a category or interview subtype to create training data.</p>
                  )}
                  {training.length > 0 && (
                    <div className="training-cards">
                      {training.map((row) => {
                        const used = row.used_in_prompt_version_id != null;
                        const from = senderName(row.sender);
                        return (
                          <article key={row.id} className="training-card">
                            <span className={`avatar soft label-${row.corrected_label}`}>
                              {initialsFrom(from)}
                            </span>
                            <span className="msg-body">
                              <span className="msg-top">
                                <span className="msg-from">
                                  <strong>{from}</strong>
                                </span>
                                <time className="msg-time">{formatWhen(row.created_at)}</time>
                              </span>
                              <span className="msg-subject">{row.subject || "(no subject)"}</span>
                              {row.snippet ? <span className="msg-snippet">{row.snippet}</span> : null}
                              <span className="msg-card-meta">
                                <span className={`label list-label label-${row.previous_label}`}>
                                  {titleForLabel(row.previous_label)}{row.previous_subtype ? ` · ${INTERVIEW_SUBTYPE_TITLES[row.previous_subtype]}` : ""}
                                </span>
                                <span className="training-arrow" aria-hidden="true">
                                  →
                                </span>
                                <span className={`label list-label label-${row.corrected_label}`}>
                                  {titleForLabel(row.corrected_label)}{row.corrected_subtype ? ` · ${INTERVIEW_SUBTYPE_TITLES[row.corrected_subtype]}` : ""}
                                </span>
                                <span className={used ? "read-pill" : "new-pill"}>
                                  {used ? "Used" : "Unused"}
                                </span>
                              </span>
                            </span>
                            {!used && (
                              <button
                                type="button"
                                className="action-btn danger training-delete"
                                aria-label={`Delete unused training example: ${row.subject || "(no subject)"}`}
                                disabled={updatingPrompt || deletingUnused || deletingTrainingId != null}
                                onClick={() => void deleteTrainingExample(row.id)}
                              >
                                {deletingTrainingId === row.id ? "Deleting..." : "Delete"}
                              </button>
                            )}
                          </article>
                        );
                      })}
                    </div>
                  )}
                  {trainingTotal > TRAINING_PAGE_SIZE && (
                    <div className="training-pager">
                      <button
                        type="button"
                        className="action-btn"
                        disabled={trainingLoading || trainingPage === 0}
                        onClick={() => setTrainingPage((page) => Math.max(0, page - 1))}
                      >
                        Previous
                      </button>
                      <span>
                        Page {trainingPage + 1} of {Math.ceil(trainingTotal / TRAINING_PAGE_SIZE)} · {trainingTotal}
                      </span>
                      <button
                        type="button"
                        className="action-btn"
                        disabled={
                          trainingLoading ||
                          trainingPage >= Math.ceil(trainingTotal / TRAINING_PAGE_SIZE) - 1
                        }
                        onClick={() => setTrainingPage((page) => page + 1)}
                      >
                        Next
                      </button>
                    </div>
                  )}
                </section>
              </div>
            )}

          </div>
        </div>
      </div>
    </main>
  );
}
