import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  deleteClassifyTrainingExample,
  deleteUnusedClassifyTraining,
  fetchClassifyPromptStatus,
  fetchClassifyTraining,
  fetchAiSettings,
  fetchOllamaModels,
  fetchOpenAiCosts,
  fetchMailboxLabelStats,
  fetchMailboxOutcomes,
  saveAiSettings,
  type AiSettings,
  type OpenAiCosts,
  type ClassifyPromptStatus,
  type ClassifyTrainingExample,
  type Mailbox,
  type MailboxLabelStats,
  type MailboxOutcomes,
} from "../api";
import { CLASSIFY_LABEL_TITLES, INTERVIEW_SUBTYPE_TITLES } from "../labels";
import {
  FONT_FAMILIES,
  FONT_SIZES,
  INBOX_TYPES,
  VIEW_MODES,
  type FontFamily,
  type FontSize,
  type InboxType,
  type ViewMode,
} from "../prefs";
import type { ThemePref } from "../theme";
import AccountSelect from "../components/AccountSelect";
import DateTimeField from "../components/DateTimeField";
import LabelPieChart from "../components/LabelPieChart";

export type SettingsSection = "accounts" | "appearance" | "ai" | "training" | "statistics";

const TRAINING_PAGE_SIZE = 20;

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

function toDatetimeLocal(value: Date): string {
  return `${value.getFullYear()}-${pad2(value.getMonth() + 1)}-${pad2(value.getDate())}T${pad2(value.getHours())}:${pad2(value.getMinutes())}`;
}

function defaultDateRange(): { from: string; to: string } {
  const now = new Date();
  const start = new Date(now);
  start.setHours(0, 0, 0, 0);
  return { from: toDatetimeLocal(start), to: toDatetimeLocal(now) };
}

function localInputToUtcIso(value: string): string {
  return new Date(value).toISOString();
}

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
  fontFamily,
  onFontFamilyChange,
  fontSize,
  onFontSizeChange,
  mailboxes,
  unusedTraining,
  updatingPrompt,
  onUpdatePrompt,
  onTrainingDeleted,
  onAddAccount,
  onRemoveAccount,
  syncingIds,
  reclassifyingIds,
  onSyncAccount,
  onStopSyncAccount,
  onReclassifyAccount,
  onStopReclassifyAccount,
  accountStatus,
  reclassifyProgress,
  initialMailboxId = null,
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
  fontFamily: FontFamily;
  onFontFamilyChange: (font: FontFamily) => void;
  fontSize: FontSize;
  onFontSizeChange: (size: FontSize) => void;
  mailboxes: Mailbox[];
  unusedTraining: number;
  updatingPrompt: boolean;
  onUpdatePrompt: () => Promise<void> | void;
  onTrainingDeleted: () => Promise<void>;
  onAddAccount: () => void;
  onRemoveAccount: (mailboxId: number) => Promise<void>;
  syncingIds: Set<number>;
  reclassifyingIds: Set<number>;
  onSyncAccount: (mailboxId: number) => Promise<void>;
  onStopSyncAccount: (mailboxId: number) => Promise<void>;
  onReclassifyAccount: (mailboxId: number) => Promise<void>;
  onStopReclassifyAccount: (mailboxId: number) => Promise<void>;
  accountStatus: Record<number, { type: "sync" | "reclassify" | "success" | "error"; message: string }>;
  reclassifyProgress: Record<number, { processed: number; total: number; failed: number }>;
  initialMailboxId?: number | null;
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
  const [openaiCosts, setOpenaiCosts] = useState<OpenAiCosts | null>(null);
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
        ollama_url: aiSettings.ollama_url,
        ollama_model: aiSettings.ollama_model,
      });
      setAiSettings(saved);
      setOpenaiKey("");
      setOpenaiAdminKey("");
      setAiNotice("AI model settings saved.");
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

  const dateDefaults = useMemo(() => defaultDateRange(), []);
  const [statsMailboxId, setStatsMailboxId] = useState<number | "all">(
    initialMailboxId ?? "all"
  );
  const [dateFrom, setDateFrom] = useState(dateDefaults.from);
  const [dateTo, setDateTo] = useState(dateDefaults.to);
  const [stats, setStats] = useState<MailboxLabelStats | null>(null);
  const [statsError, setStatsError] = useState<string | null>(null);
  const [statsLoading, setStatsLoading] = useState(false);
  const [outcomeLabel, setOutcomeLabel] = useState<"application_confirmation" | "rejected_closed" | "interview_invitation" | "interview_scheduled">("interview_invitation");
  const [outcomePage, setOutcomePage] = useState(0);
  const [outcomes, setOutcomes] = useState<MailboxOutcomes | null>(null);
  const [outcomesLoading, setOutcomesLoading] = useState(false);
  const [outcomeCompanyQuery, setOutcomeCompanyQuery] = useState("");
  const [outcomePieOpen, setOutcomePieOpen] = useState(false);
  const [outcomePieCounts, setOutcomePieCounts] = useState<Record<string, number> | null>(null);
  const [outcomePieLoading, setOutcomePieLoading] = useState(false);
  const OUTCOME_PAGE_SIZE = 20;

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

  async function loadStats(
    event?: FormEvent,
    mailboxId: number | "all" = statsMailboxId
  ) {
    event?.preventDefault();
    if (dateFrom > dateTo) {
      setStatsError("From must be on or before To");
      return;
    }
    const fromIso = localInputToUtcIso(dateFrom);
    const toIso = localInputToUtcIso(dateTo);
    setStatsLoading(true);
    setStatsError(null);
    try {
      setStats(await fetchMailboxLabelStats(mailboxId, fromIso, toIso));
    } catch (err) {
      setStats(null);
      setStatsError(err instanceof Error ? err.message : "Failed to load statistics");
    } finally {
      setStatsLoading(false);
    }
  }

  async function loadOutcomes(mailboxId = statsMailboxId, page = outcomePage, label = outcomeLabel) {
    if (mailboxId === "all") { setOutcomes(null); return; }
    setOutcomesLoading(true);
    try {
      setOutcomes(await fetchMailboxOutcomes(mailboxId, localInputToUtcIso(dateFrom), localInputToUtcIso(dateTo), { label, limit: OUTCOME_PAGE_SIZE, offset: page * OUTCOME_PAGE_SIZE }));
    } catch (err) { setStatsError(err instanceof Error ? err.message : "Failed to load extracted outcomes"); }
    finally { setOutcomesLoading(false); }
  }

  async function copyOutcomes() {
    if (!outcomes || statsMailboxId === "all") return;
    const pages = await Promise.all(
      Array.from({ length: Math.ceil(outcomes.total / 200) }, (_, index) =>
        fetchMailboxOutcomes(statsMailboxId, localInputToUtcIso(dateFrom), localInputToUtcIso(dateTo), { label: outcomeLabel, limit: 200, offset: index * 200 })
      )
    );
    const rows = pages.flatMap((page) => page.items);
    await navigator.clipboard.writeText(["Company\tRole\tSubject", ...rows.map((item) => `${item.company}\t${item.role}\t${item.subject}`)].join("\n"));
  }

  const visibleOutcomes = useMemo(() => {
    const query = outcomeCompanyQuery.trim().toLocaleLowerCase();
    return !query ? outcomes?.items ?? [] : (outcomes?.items ?? []).filter((item) => item.company.toLocaleLowerCase().includes(query));
  }, [outcomes, outcomeCompanyQuery]);

  async function loadOutcomePie() {
    if (statsMailboxId === "all") return;
    setOutcomePieLoading(true);
    try {
      const labels: ("application_confirmation" | "rejected_closed" | "interview_invitation" | "interview_scheduled")[] = ["application_confirmation", "rejected_closed", "interview_invitation", "interview_scheduled"];
      const fromIso = localInputToUtcIso(dateFrom);
      const toIso = localInputToUtcIso(dateTo);
      const pages = await Promise.all(labels.map((label) => fetchMailboxOutcomes(statsMailboxId, fromIso, toIso, { label, limit: 1, offset: 0 })));
      setOutcomePieCounts(Object.fromEntries(pages.map((page, index) => [labels[index], page.total])));
    } catch (err) { setStatsError(err instanceof Error ? err.message : "Failed to load extraction statistics"); }
    finally { setOutcomePieLoading(false); }
  }

  useEffect(() => {
    if (section !== "statistics") return;
    void loadStats();
    // Load once when opening Statistics.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [section]);

  useEffect(() => {
    if (section !== "statistics" || statsMailboxId === "all") return;
    void loadOutcomes(statsMailboxId, outcomePage, outcomeLabel);
    // Outcome browsing intentionally covers the full account history.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [section, statsMailboxId, outcomeLabel, outcomePage]);

  async function handleUpdatePrompt() {
    await onUpdatePrompt();
  }

  const navItems: { id: SettingsSection; label: string }[] = [
    { id: "accounts", label: "Accounts" },
    { id: "appearance", label: "Appearance" },
    { id: "ai", label: "AI model" },
    { id: "training", label: "Training" },
    { id: "statistics", label: "Statistics" },
  ];

  return (
    <main className="settings-page" aria-labelledby="settings-title">
      <div className="settings-shell">
        <header className="settings-top">
          <h2 id="settings-title">Settings</h2>
          <button type="button" className="action-btn" onClick={onClose}>
            Back to inbox
          </button>
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
            {section === "accounts" && (
              <div className="settings-panel">
                <section className="settings-block">
                  <div className="settings-block-heading">
                    <div>
                      <h3>Email accounts</h3>
                      <p className="settings-help">Add connected inboxes or remove accounts you no longer want to sync.</p>
                    </div>
                    <button type="button" className="action-btn primary" onClick={onAddAccount}>+ Add an account</button>
                  </div>
                  <div className="settings-account-list">
                    {mailboxes.map((mailbox) => {
                      const isWorking = syncingIds.has(mailbox.id) || reclassifyingIds.has(mailbox.id);
                      const activeStatus = accountStatus[mailbox.id];
                      return (
                      <div className="settings-account-row" key={mailbox.id}>
                        <div className="settings-account-info">
                          <strong>{mailbox.email_address}</strong>
                          <span>{mailbox.provider === "google" ? "Google Gmail" : "Microsoft Outlook"}</span>
                        </div>
                        <div className="settings-account-actions">
                          <button
                            type="button"
                            className={syncingIds.has(mailbox.id) ? "action-btn busy" : "action-btn"}
                            disabled={reclassifyingIds.has(mailbox.id) || removingMailboxId != null}
                            onClick={() => void (syncingIds.has(mailbox.id) ? onStopSyncAccount(mailbox.id) : onSyncAccount(mailbox.id))}
                          >
                            {syncingIds.has(mailbox.id) ? "Stop sync" : "Sync"}
                          </button>
                          <button
                            type="button"
                            className={reclassifyingIds.has(mailbox.id) ? "action-btn busy" : "action-btn"}
                            disabled={syncingIds.has(mailbox.id) || removingMailboxId != null}
                            onClick={() => void (reclassifyingIds.has(mailbox.id) ? onStopReclassifyAccount(mailbox.id) : onReclassifyAccount(mailbox.id))}
                          >
                            {reclassifyingIds.has(mailbox.id) ? "Stop reclassify" : "Reclassify"}
                          </button>
                          <button
                            type="button"
                            className="action-btn danger"
                            disabled={syncingIds.has(mailbox.id) || reclassifyingIds.has(mailbox.id) || removingMailboxId != null}
                            onClick={() => {
                              if (!window.confirm(`Remove ${mailbox.email_address} from this app?`)) return;
                              setRemovingMailboxId(mailbox.id);
                              void onRemoveAccount(mailbox.id).finally(() => setRemovingMailboxId(null));
                            }}
                          >
                            {removingMailboxId === mailbox.id ? "Removing…" : "Remove"}
                          </button>
                        </div>
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
                  <div className="seg-control" role="radiogroup" aria-label="View mode">
                    {VIEW_MODES.map((mode) => (
                      <button
                        key={mode}
                        type="button"
                        role="radio"
                        aria-checked={viewMode === mode}
                        className={viewMode === mode ? "seg-btn active" : "seg-btn"}
                        onClick={() => onViewModeChange(mode)}
                      >
                        {mode === "list" ? "List" : mode === "card" ? "Card" : "Table"}
                      </button>
                    ))}
                  </div>
                </section>
                <section className="settings-block">
                  <h3>Inbox type</h3>
                  <p className="settings-help">Order of messages in the inbox list.</p>
                  <div className="seg-control" role="radiogroup" aria-label="Inbox type">
                    {INBOX_TYPES.map((type) => (
                      <button
                        key={type}
                        type="button"
                        role="radio"
                        aria-checked={inboxType === type}
                        className={inboxType === type ? "seg-btn active" : "seg-btn"}
                        onClick={() => onInboxTypeChange(type)}
                      >
                        {type === "default" ? "Default" : "Unread first"}
                      </button>
                    ))}
                  </div>
                </section>
                <section className="settings-block">
                  <h3>Theme</h3>
                  <p className="settings-help">Color scheme for the app.</p>
                  <div className="seg-control" role="radiogroup" aria-label="Theme">
                    {(["system", "dark", "light"] as ThemePref[]).map((item) => (
                      <button
                        key={item}
                        type="button"
                        role="radio"
                        aria-checked={themePref === item}
                        className={themePref === item ? "seg-btn active" : "seg-btn"}
                        onClick={() => onThemeChange(item)}
                      >
                        {item === "system" ? "System" : item === "dark" ? "Dark" : "Light"}
                      </button>
                    ))}
                  </div>
                </section>
                <section className="settings-block">
                  <h3>Font</h3>
                  <p className="settings-help">Typeface and size used across the app.</p>
                  <label className="settings-field">
                    Family
                    <select
                      value={fontFamily}
                      onChange={(event) => onFontFamilyChange(event.target.value as FontFamily)}
                    >
                      {FONT_FAMILIES.map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.label}
                        </option>
                      ))}
                    </select>
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
                        {(["openai", "ollama"] as const).map((provider) => (
                          <button key={provider} type="button" role="radio"
                            aria-checked={aiSettings.provider === provider}
                            className={aiSettings.provider === provider ? "model-card active" : "model-card"}
                            onClick={() => setAiSettings({ ...aiSettings, provider })}>
                            <strong>{provider === "openai" ? "OpenAI" : "Local AI (Ollama)"}</strong>
                            <span>{provider === "openai" ? "Cloud model using your API key" : "Private model on your local server"}</span>
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
                      ) : (
                        <div className="ai-fields">
                          <label className="settings-field">Ollama server
                            <input value={aiSettings.ollama_url} onChange={(e) => setAiSettings({ ...aiSettings, ollama_url: e.target.value })} />
                          </label>
                          <label className="settings-field">Ollama model
                            <select value={aiSettings.ollama_model} onChange={(e) => setAiSettings({ ...aiSettings, ollama_model: e.target.value })}>
                              {!ollamaModels.includes(aiSettings.ollama_model) && <option value={aiSettings.ollama_model}>{aiSettings.ollama_model}</option>}
                              {ollamaModels.map((model) => (
                                <option key={model} value={model}>
                                  {model}{model === "qwen2.5:14b-instruct" ? " — Recommended" : ""}
                                </option>
                              ))}
                            </select>
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

            {section === "statistics" && (
              <div className="settings-panel">
                <section className="settings-block">
                  <h3>Category statistics</h3>
                  <p className="settings-help">
                    Counts from the start of today through now. All accounts shows every mailbox.
                    The pie chart shows the category distribution for the selected account or all accounts.
                  </p>
                  <form
                    className="stats-form stats-filter-card"
                    onSubmit={(event) => {
                      void loadStats(event);
                      void loadOutcomes();
                    }}
                  >
                    <AccountSelect
                      value={statsMailboxId}
                      mailboxes={mailboxes}
                      onChange={(value) => {
                        setStatsMailboxId(value);
                        setStats(null);
                        setOutcomePage(0);
                        void loadStats(undefined, value);
                        void loadOutcomes(value, 0);
                      }}
                    />
                    <DateTimeField label="From" value={dateFrom} onChange={setDateFrom} />
                    <DateTimeField label="To" value={dateTo} onChange={setDateTo} />
                    <button type="submit" className="action-btn" disabled={statsLoading}>
                      {statsLoading ? "Loading…" : "Search"}
                    </button>
                  </form>
                  {statsError && <p className="error">{statsError}</p>}
                  {stats && stats.total === 0 && !statsError && (
                    <p className="hint">No emails in this date range.</p>
                  )}
                  {stats && (
                    <div className="stats-visuals stats-account">
                      <LabelPieChart counts={stats.label_counts} />
                    </div>
                  )}
                  {statsMailboxId !== "all" && (
                    <section className="outcome-browser">
                      <div className="outcome-toolbar">
                        <div>
                          <h4>AI company & role extraction</h4>
                          <p className="settings-help">Company and role saved from the selected recruiting category.</p>
                        </div>
                      </div>
                      <div className="outcome-browser-actions">
                          <div className="outcome-tabs" role="tablist" aria-label="Extraction category">{([ ["interview_invitation", "Interview Invitation"], ["interview_scheduled", "Interview Scheduled"], ["application_confirmation", "Applied"], ["rejected_closed", "Rejected"] ] as const).map(([label, title]) => <button key={label} type="button" role="tab" aria-selected={outcomeLabel === label} className={outcomeLabel === label ? "active" : ""} onClick={() => { setOutcomeLabel(label); setOutcomePage(0); void loadOutcomes(statsMailboxId, 0, label); }}>{title}</button>)}</div>
                          <input className="modern-control outcome-company-search" type="search" value={outcomeCompanyQuery} onChange={(event) => setOutcomeCompanyQuery(event.target.value)} placeholder="Search company" aria-label="Search company name" />
                          <button type="button" className="settings-btn outcome-stats-btn" title="Show extracted-data statistics" aria-label="Show extracted-data statistics" aria-expanded={outcomePieOpen} onClick={() => { setOutcomePieOpen(true); void loadOutcomePie(); }}>◔</button>
                          <button type="button" className="settings-btn outcome-copy-btn" title="Copy all results" aria-label="Copy all results" disabled={!outcomes?.items.length} onClick={() => void copyOutcomes()}>⧉</button>
                      </div>
                      {outcomesLoading && <p className="hint">Loading extracted data…</p>}
                      {!outcomesLoading && outcomes && (
                        <div className="outcome-table-wrap"><table className="settings-table outcome-table"><thead><tr><th>Date</th><th>Company</th><th>Role</th><th>Subject</th></tr></thead><tbody>{visibleOutcomes.map((item, index) => <tr key={`${item.subject}-${index}`}><td>{formatWhen(item.received_at) || "—"}</td><td>{item.company || "—"}</td><td>{item.role || "—"}</td><td>{item.subject}</td></tr>)}</tbody></table></div>
                      )}
                      {outcomes && outcomes.total === 0 && !outcomesLoading && <p className="hint">No extracted records found for this account and category.</p>}
                      {outcomes && outcomes.total > OUTCOME_PAGE_SIZE && <div className="pagination"><button type="button" className="action-btn" disabled={outcomePage === 0} onClick={() => { const page = outcomePage - 1; setOutcomePage(page); void loadOutcomes(statsMailboxId, page); }}>Previous</button><span>Page {outcomePage + 1} of {Math.ceil(outcomes.total / OUTCOME_PAGE_SIZE)}</span><button type="button" className="action-btn" disabled={(outcomePage + 1) * OUTCOME_PAGE_SIZE >= outcomes.total} onClick={() => { const page = outcomePage + 1; setOutcomePage(page); void loadOutcomes(statsMailboxId, page); }}>Next</button></div>}
                    </section>
                  )}
                  {outcomePieOpen && (
                    <div className="outcome-pie-modal-backdrop" role="presentation" onMouseDown={() => setOutcomePieOpen(false)}>
                      <section className="outcome-pie-modal" role="dialog" aria-modal="true" aria-labelledby="outcome-pie-title" onMouseDown={(event) => event.stopPropagation()}>
                        <header><div><span className="stats-eyebrow">AI extraction</span><h3 id="outcome-pie-title">Company & role statistics</h3></div><button type="button" className="dialog-close" aria-label="Close statistics" onClick={() => setOutcomePieOpen(false)}>×</button></header>
                        {outcomePieLoading ? <p className="hint">Loading extraction statistics…</p> : outcomePieCounts && <LabelPieChart counts={outcomePieCounts} labels={["application_confirmation", "rejected_closed", "interview_invitation", "interview_scheduled"]} />}
                      </section>
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
