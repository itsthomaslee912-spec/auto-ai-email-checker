import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import {
  fetchMailboxLabelStats,
  fetchMailboxOutcomes,
  type Mailbox,
  type MailboxLabelStats,
  type MailboxOutcomes,
  type OutcomeLabel,
} from "../api";
import AccountSelect from "../components/AccountSelect";
import LabelPieChart from "../components/LabelPieChart";
import ModernDateTimePicker from "../components/ModernDateTimePicker";
import { CLASSIFY_LABEL_TITLES } from "../labels";

const OUTCOME_PAGE_SIZE = 20;
const OUTCOME_LABELS = [
  ["interview_invitation", "Interview Invitation"],
  ["interview_scheduled", "Interview Scheduled"],
  ["application_confirmation", "Applied"],
  ["rejected_closed", "Rejected"],
] as const satisfies readonly (readonly [OutcomeLabel, string])[];

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
  const parsed = new Date(hasZone ? value : `${value}Z`);
  if (Number.isNaN(parsed.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(parsed);
}

export default function StatisticsPage({
  active,
  mailboxes,
  initialMailboxId,
}: {
  active: boolean;
  mailboxes: Mailbox[];
  initialMailboxId: number | null;
}) {
  const defaults = useMemo(() => defaultDateRange(), []);
  const [mailboxId, setMailboxId] = useState<number | "all">(initialMailboxId ?? "all");
  const [dateFrom, setDateFrom] = useState(defaults.from);
  const [dateTo, setDateTo] = useState(defaults.to);
  const [stats, setStats] = useState<MailboxLabelStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [outcomeLabel, setOutcomeLabel] = useState<OutcomeLabel>("interview_invitation");
  const [outcomePage, setOutcomePage] = useState(0);
  const [outcomes, setOutcomes] = useState<MailboxOutcomes | null>(null);
  const [outcomesLoading, setOutcomesLoading] = useState(false);
  const [companyQuery, setCompanyQuery] = useState("");
  const [outcomePieOpen, setOutcomePieOpen] = useState(false);
  const [outcomePieCounts, setOutcomePieCounts] = useState<Record<string, number> | null>(null);
  const [outcomePieLoading, setOutcomePieLoading] = useState(false);

  const loadStats = useCallback(async (selectedMailbox: number | "all" = mailboxId) => {
    if (dateFrom > dateTo) {
      setError("From must be on or before To");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      setStats(await fetchMailboxLabelStats(selectedMailbox, localInputToUtcIso(dateFrom), localInputToUtcIso(dateTo)));
    } catch (err) {
      setStats(null);
      setError(err instanceof Error ? err.message : "Failed to load statistics");
    } finally {
      setLoading(false);
    }
  }, [dateFrom, dateTo, mailboxId]);

  const loadOutcomes = useCallback(async (
    selectedMailbox: number | "all" = mailboxId,
    page = outcomePage,
    label: OutcomeLabel = outcomeLabel,
  ) => {
    if (selectedMailbox === "all") {
      setOutcomes(null);
      return;
    }
    setOutcomesLoading(true);
    try {
      setOutcomes(await fetchMailboxOutcomes(
        selectedMailbox,
        localInputToUtcIso(dateFrom),
        localInputToUtcIso(dateTo),
        { label, limit: OUTCOME_PAGE_SIZE, offset: page * OUTCOME_PAGE_SIZE },
      ));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load extracted outcomes");
    } finally {
      setOutcomesLoading(false);
    }
  }, [dateFrom, dateTo, mailboxId, outcomeLabel, outcomePage]);

  useEffect(() => {
    if (!active) return;
    void loadStats();
    // Initial load is intentionally tied to opening the page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  useEffect(() => {
    if (!active || mailboxId === "all") return;
    void loadOutcomes(mailboxId, outcomePage, outcomeLabel);
    // The selected date range is applied when Search is submitted.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, mailboxId, outcomeLabel, outcomePage]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !outcomePieOpen) return;
      event.preventDefault();
      setOutcomePieOpen(false);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [outcomePieOpen]);

  async function search(event: FormEvent) {
    event.preventDefault();
    setOutcomePage(0);
    await Promise.all([loadStats(), loadOutcomes(mailboxId, 0, outcomeLabel)]);
  }

  async function copyOutcomes() {
    if (!outcomes || mailboxId === "all") return;
    const pages = await Promise.all(
      Array.from({ length: Math.ceil(outcomes.total / 200) }, (_, index) =>
        fetchMailboxOutcomes(mailboxId, localInputToUtcIso(dateFrom), localInputToUtcIso(dateTo), {
          label: outcomeLabel,
          limit: 200,
          offset: index * 200,
        }),
      ),
    );
    const rows = pages.flatMap((page) => page.items);
    await navigator.clipboard.writeText(
      ["Company\tRole\tSubject", ...rows.map((item) => `${item.company}\t${item.role}\t${item.subject}`)].join("\n"),
    );
  }

  async function loadOutcomePie() {
    if (mailboxId === "all") return;
    setOutcomePieLoading(true);
    setOutcomePieOpen(true);
    try {
      const labels = OUTCOME_LABELS.map(([label]) => label);
      const pages = await Promise.all(labels.map((label) => fetchMailboxOutcomes(
        mailboxId,
        localInputToUtcIso(dateFrom),
        localInputToUtcIso(dateTo),
        { label, limit: 1, offset: 0 },
      )));
      setOutcomePieCounts(Object.fromEntries(pages.map((page, index) => [labels[index], page.total])));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load extraction statistics");
    } finally {
      setOutcomePieLoading(false);
    }
  }

  const visibleOutcomes = useMemo(() => {
    const query = companyQuery.trim().toLocaleLowerCase();
    return !query ? outcomes?.items ?? [] : (outcomes?.items ?? []).filter((item) => item.company.toLocaleLowerCase().includes(query));
  }, [companyQuery, outcomes]);

  const summary = useMemo(() => {
    const entries = Object.entries(stats?.label_counts ?? {}).sort((a, b) => b[1] - a[1]);
    const leading = entries.find(([, count]) => count > 0);
    return {
      categories: entries.filter(([, count]) => count > 0).length,
      leadingName: leading ? CLASSIFY_LABEL_TITLES[leading[0] as keyof typeof CLASSIFY_LABEL_TITLES] ?? leading[0] : "No data",
      leadingCount: leading?.[1] ?? 0,
    };
  }, [stats]);

  return (
    <main className="statistics-page">
      <header className="statistics-header">
        <div className="monitoring-heading">
          <p className="monitoring-eyebrow">Admin analytics</p>
          <h1>Email statistics</h1>
          <p>Explore category distribution and extracted recruiting outcomes.</p>
        </div>
      </header>

      <form className="statistics-filters" onSubmit={search}>
        <AccountSelect
          value={mailboxId}
          mailboxes={mailboxes}
          onChange={(value) => {
            setMailboxId(value);
            setOutcomePage(0);
            setStats(null);
            void loadStats(value);
          }}
        />
        <ModernDateTimePicker label="From" value={dateFrom} onChange={setDateFrom} />
        <ModernDateTimePicker label="To" value={dateTo} onChange={setDateTo} />
        <button type="submit" className="statistics-search" disabled={loading}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m16.5 16.5 4 4" /></svg>
          {loading ? "Loading…" : "Search"}
        </button>
      </form>

      {error && <div className="monitoring-error" role="alert"><strong>Statistics unavailable</strong><span>{error}</span></div>}

      <section className="statistics-kpis" aria-label="Statistics summary">
        <article><span>Emails in range</span><strong>{(stats?.total ?? 0).toLocaleString()}</strong><small>Across the selected account scope</small></article>
        <article><span>Active categories</span><strong>{summary.categories}</strong><small>Categories containing email</small></article>
        <article><span>Largest category</span><strong className="is-label">{summary.leadingName}</strong><small>{summary.leadingCount.toLocaleString()} emails</small></article>
        <article><span>Connected accounts</span><strong>{mailboxes.length.toLocaleString()}</strong><small>{mailboxId === "all" ? "All accounts selected" : "One account selected"}</small></article>
      </section>

      <section className="statistics-section">
        <div className="monitoring-section-title"><div><p>Classification</p><h2>Category performance</h2></div><span>{stats?.total.toLocaleString() ?? 0} emails</span></div>
        {loading && !stats ? (
          <div className="statistics-loading"><span className="monitoring-spinner" />Loading category statistics…</div>
        ) : stats ? (
          <LabelPieChart counts={stats.label_counts} />
        ) : null}
      </section>

      {mailboxId === "all" ? (
        <section className="statistics-account-prompt">
          <span aria-hidden="true"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="3" /><path d="M8 9h8M8 13h5" /></svg></span>
          <div><strong>Select one account for recruiting outcomes</strong><p>Company and role details are available for an individual mailbox.</p></div>
        </section>
      ) : (
        <section className="statistics-section statistics-outcomes">
          <div className="monitoring-section-title"><div><p>AI extraction</p><h2>Company and role outcomes</h2></div><span>{outcomes?.total.toLocaleString() ?? 0} records</span></div>
          <div className="statistics-outcome-controls">
            <div className="outcome-tabs" role="tablist" aria-label="Extraction category">
              {OUTCOME_LABELS.map(([label, title]) => <button key={label} type="button" role="tab" aria-selected={outcomeLabel === label} className={outcomeLabel === label ? "active" : ""} onClick={() => { setOutcomeLabel(label); setOutcomePage(0); }}>{title}</button>)}
            </div>
            <input className="modern-control outcome-company-search" type="search" value={companyQuery} onChange={(event) => setCompanyQuery(event.target.value)} placeholder="Search company" aria-label="Search company name" />
            <button type="button" className="statistics-icon-btn" aria-label="Show extracted-data statistics" onClick={() => void loadOutcomePie()}>
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v9h9A9 9 0 1 1 12 3Z" /><path d="M15 3.5A8 8 0 0 1 20.5 9H15V3.5Z" /></svg>
            </button>
            <button type="button" className="statistics-icon-btn" aria-label="Copy all results" disabled={!outcomes?.items.length} onClick={() => void copyOutcomes()}>
              <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" /></svg>
            </button>
          </div>
          {outcomesLoading ? <div className="statistics-loading"><span className="monitoring-spinner" />Loading extracted data…</div> : outcomes && outcomes.total > 0 ? (
            <div className="statistics-table-wrap"><table className="settings-table outcome-table"><thead><tr><th>Date</th><th>Company</th><th>Role</th><th>Subject</th></tr></thead><tbody>{visibleOutcomes.map((item, index) => <tr key={`${item.subject}-${index}`}><td>{formatWhen(item.received_at) || "—"}</td><td>{item.company || "—"}</td><td>{item.role || "—"}</td><td>{item.subject}</td></tr>)}</tbody></table></div>
          ) : <p className="statistics-empty">No extracted records found for this account and category.</p>}
          {outcomes && outcomes.total > OUTCOME_PAGE_SIZE && <div className="pagination"><button type="button" className="action-btn" disabled={outcomePage === 0} onClick={() => setOutcomePage((page) => page - 1)}>Previous</button><span>Page {outcomePage + 1} of {Math.ceil(outcomes.total / OUTCOME_PAGE_SIZE)}</span><button type="button" className="action-btn" disabled={(outcomePage + 1) * OUTCOME_PAGE_SIZE >= outcomes.total} onClick={() => setOutcomePage((page) => page + 1)}>Next</button></div>}
        </section>
      )}

      {outcomePieOpen && (
        <div className="outcome-pie-modal-backdrop" role="presentation" onMouseDown={() => setOutcomePieOpen(false)}>
          <section className="outcome-pie-modal" role="dialog" aria-modal="true" aria-labelledby="outcome-pie-title" onMouseDown={(event) => event.stopPropagation()}>
            <header><div><span className="stats-eyebrow">AI extraction</span><h3 id="outcome-pie-title">Company &amp; role statistics</h3></div><button type="button" className="dialog-close" aria-label="Close statistics" onClick={() => setOutcomePieOpen(false)}>×</button></header>
            {outcomePieLoading ? <p className="hint">Loading extraction statistics…</p> : outcomePieCounts && <LabelPieChart counts={outcomePieCounts} labels={OUTCOME_LABELS.map(([label]) => label)} />}
          </section>
        </div>
      )}
    </main>
  );
}
