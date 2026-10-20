import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { fetchMonitoringSnapshot, openEvents, type MonitoringService, type MonitoringSnapshot } from "../api";

type ViewMode = "dashboard" | "architecture";
const VIEW_KEY = "monitoring-view";

// Which pipeline stages light up when a given live event arrives. Keys match
// the `data-stage` of each node below so the diagram shows real data flow.
const FLOW_STAGES = ["providers", "sync", "postgres", "relay", "rabbitmq", "classification", "notification", "api", "browser"] as const;
type Stage = (typeof FLOW_STAGES)[number];
const EVENT_FLOW: Record<string, Stage[]> = {
  "sync.progress": ["providers", "sync", "postgres", "relay", "rabbitmq"],
  "sync.done": ["providers", "sync", "postgres", "relay", "rabbitmq"],
  "email.classified": ["rabbitmq", "classification", "postgres", "notification", "api", "browser"],
  "reclassify.progress": ["classification", "postgres", "notification", "api", "browser"],
  "reclassify.done": ["classification", "postgres", "notification", "api", "browser"],
  "calendar.changed": ["classification", "postgres", "api", "browser"],
};
const FLOW_HOLD_MS = 1800;

function relativeTime(value: string | null): string {
  if (!value) return "No heartbeat received";
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "Unknown";
  const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
  if (seconds < 10) return "Just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.round(minutes / 60)}h ago`;
}

function number(value: number): string {
  return new Intl.NumberFormat().format(value);
}

function StatusDot({ status }: { status: string }) {
  return <span className={`monitor-status-dot status-${status}`} aria-hidden="true" />;
}

/* ---- Dashboard (classic) view ---- */

function ServiceCard({ service }: { service: MonitoringService }) {
  return (
    <article className={`monitor-service-card status-${service.status}`}>
      <div className="monitor-service-heading">
        <span className="monitor-service-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="6" rx="2" /><rect x="3" y="14" width="18" height="6" rx="2" /><path d="M7 7h.01M7 17h.01M11 7h7M11 17h7" /></svg>
        </span>
        <div><h3>{service.label}</h3><p>{service.description}</p></div>
      </div>
      <div className="monitor-service-meta">
        <span className="monitor-status"><StatusDot status={service.status} />{service.status}</span>
        <span>{service.replicas} live {service.replicas === 1 ? "replica" : "replicas"}</span>
        <span>{relativeTime(service.last_seen_at)}</span>
      </div>
    </article>
  );
}

/* ---- Architecture (diagram) view ---- */

const ICONS: Record<string, ReactNode> = {
  provider: <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 7 9 6 9-6" /></>,
  sync: <><path d="M20 7v5h-5M4 17v-5h5" /><path d="M6.1 8.2A7 7 0 0 1 18.8 7L20 12M4 12l1.2 5A7 7 0 0 0 17.9 15.8" /></>,
  relay: <><path d="M4 12h12M12 6l6 6-6 6" /><circle cx="4" cy="12" r="1.6" /></>,
  broker: <><rect x="3" y="4" width="18" height="6" rx="2" /><rect x="3" y="14" width="18" height="6" rx="2" /><path d="M7 7h.01M7 17h.01" /></>,
  classify: <><path d="M11 3.5l1.7 4.4 4.4 1.7-4.4 1.7L11 15.7l-1.7-4.4-4.4-1.7 4.4-1.7z" /><path d="M18 14.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z" /></>,
  notify: <><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.7 21a2 2 0 0 1-3.4 0" /></>,
  api: <><rect x="3" y="4" width="18" height="16" rx="3" /><path d="M8 9l-2 3 2 3M16 9l2 3-2 3" /></>,
  client: <><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M8 20h8M12 16v4" /></>,
  store: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7" /></>,
};

function Icon({ name }: { name: keyof typeof ICONS }) {
  return <svg viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>;
}

function ArchNode({
  icon, title, subtitle, status, statusLabel, meta, variant, live,
}: {
  icon: keyof typeof ICONS;
  title: string;
  subtitle: string;
  status: string;
  statusLabel?: string;
  meta?: string;
  variant?: "edge" | "store" | "endpoint";
  live?: boolean;
}) {
  return (
    <article className={`arch-node status-${status}${variant ? ` is-${variant}` : ""}${live ? " is-live" : ""}`} role="listitem">
      <span className="arch-node-icon" aria-hidden="true"><Icon name={icon} /></span>
      <div className="arch-node-body">
        <h3>{title}</h3>
        <p>{subtitle}</p>
      </div>
      <div className="arch-node-side">
        <span className="arch-node-status"><StatusDot status={status} />{statusLabel ?? status}</span>
        {meta && <span className="arch-node-meta">{meta}</span>}
      </div>
    </article>
  );
}

function ArchEdge({ label, count, error, live }: { label: string; count?: number; error?: number; live?: boolean }) {
  return (
    <div className={`arch-edge${live ? " is-live" : ""}`} aria-hidden="true">
      <span className="arch-edge-chip">
        {label}
        {typeof count === "number" && count > 0 && <b className="arch-edge-badge">{number(count)}</b>}
        {typeof error === "number" && error > 0 && <b className="arch-edge-badge is-error">{number(error)} failed</b>}
      </span>
      <svg className="arch-edge-arrow" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v15M6 13l6 6 6-6" /></svg>
    </div>
  );
}

export default function MonitoringPage({ active }: { active: boolean }) {
  const [snapshot, setSnapshot] = useState<MonitoringSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<ViewMode>(() => {
    try {
      return localStorage.getItem(VIEW_KEY) === "dashboard" ? "dashboard" : "architecture";
    } catch {
      return "architecture";
    }
  });

  const chooseView = useCallback((next: ViewMode) => {
    setView(next);
    try { localStorage.setItem(VIEW_KEY, next); } catch { /* storage unavailable */ }
  }, []);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true);
    try {
      const next = await fetchMonitoringSnapshot();
      setSnapshot(next);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load monitoring data");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    if (!active) return;
    void load(true);
    const timer = window.setInterval(() => void load(true), 15_000);
    return () => window.clearInterval(timer);
  }, [active, load]);

  const servicesHealthy = useMemo(
    () => snapshot?.services.filter((service) => service.status === "healthy").length ?? 0,
    [snapshot],
  );
  const svc = useCallback(
    (name: string): MonitoringService | undefined => snapshot?.services.find((s) => s.name === name),
    [snapshot],
  );
  const nodeMeta = (service?: MonitoringService): string =>
    service ? `${service.replicas} ${service.replicas === 1 ? "replica" : "replicas"} · ${relativeTime(service.last_seen_at)}` : "No data";

  // Live data flow: subscribe to the shared event stream and briefly light up
  // the pipeline stages each event passes through, so the diagram animates
  // actual traffic (a synced email, a classification, a calendar update).
  const pulsesRef = useRef<Record<string, number>>({});
  const [, setFlowTick] = useState(0);
  useEffect(() => {
    if (!active || view !== "architecture") return;
    const stream = openEvents();
    const handlers: Array<[string, EventListener]> = [];
    const bump = (stages: Stage[]) => {
      const until = Date.now() + FLOW_HOLD_MS;
      for (const stage of stages) pulsesRef.current[stage] = until;
      setFlowTick((tick) => tick + 1);
    };
    for (const [eventName, stages] of Object.entries(EVENT_FLOW)) {
      const handler: EventListener = () => bump(stages);
      stream.addEventListener(eventName, handler);
      handlers.push([eventName, handler]);
    }
    const ticker = window.setInterval(() => {
      const now = Date.now();
      let changed = false;
      for (const key of Object.keys(pulsesRef.current)) {
        if (pulsesRef.current[key] <= now) { delete pulsesRef.current[key]; changed = true; }
      }
      if (changed) setFlowTick((tick) => tick + 1);
    }, 350);
    return () => {
      handlers.forEach(([name, handler]) => stream.removeEventListener(name, handler));
      stream.close();
      window.clearInterval(ticker);
    };
  }, [active, view]);

  const flowNow = Date.now();
  const liveStage = (stage: Stage): boolean => (pulsesRef.current[stage] ?? 0) > flowNow;
  const liveEdge = (from: Stage, to: Stage): boolean => liveStage(from) || liveStage(to);
  const flowing = FLOW_STAGES.some(liveStage);

  if (loading && !snapshot) {
    return <main className="monitoring-page"><div className="monitoring-loading"><span className="monitoring-spinner" />Loading system health…</div></main>;
  }

  return (
    <main className="monitoring-page">
      <header className="monitoring-header">
        <div className="monitoring-heading">
          <p className="monitoring-eyebrow">Admin operations</p>
          <h1>{view === "architecture" ? "System architecture" : "System monitoring"}</h1>
          <p>
            {view === "architecture"
              ? "Live health of every microservice in the email pipeline, with queue depth on each hop."
              : "Live service health, processing queues, and account delivery problems."}
          </p>
        </div>
        <div className="monitoring-header-actions">
          <div className="monitoring-viewtoggle" role="tablist" aria-label="Monitoring view">
            <button type="button" role="tab" aria-selected={view === "dashboard"} className={view === "dashboard" ? "is-active" : ""} onClick={() => chooseView("dashboard")}>
              <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="7" height="9" rx="1.5" /><rect x="14" y="3" width="7" height="5" rx="1.5" /><rect x="14" y="12" width="7" height="9" rx="1.5" /><rect x="3" y="16" width="7" height="5" rx="1.5" /></svg>
              Dashboard
            </button>
            <button type="button" role="tab" aria-selected={view === "architecture"} className={view === "architecture" ? "is-active" : ""} onClick={() => chooseView("architecture")}>
              <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="4" rx="1" /><rect x="3" y="17" width="6" height="4" rx="1" /><rect x="15" y="17" width="6" height="4" rx="1" /><path d="M12 7v4M12 11H6v6M12 11h6v6" /></svg>
              Architecture
            </button>
          </div>
          {snapshot && <span className="monitoring-updated">Updated {relativeTime(snapshot.generated_at)}</span>}
          <button type="button" className="monitoring-refresh" onClick={() => void load()} disabled={refreshing}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5" /><path d="M6.1 8.2A7 7 0 0 1 18.8 7L20 12M4 12l1.2 5A7 7 0 0 0 17.9 15.8" /></svg>
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </header>

      {error && <div className="monitoring-error" role="alert"><strong>Monitoring unavailable</strong><span>{error}</span></div>}

      {snapshot && <>
        <section className={`monitoring-overview status-${snapshot.overall_status}`} aria-label="System status">
          <div className="monitoring-overview-mark"><StatusDot status={snapshot.overall_status} /></div>
          <div>
            <span>Platform status</span>
            <h2>{snapshot.overall_status === "healthy" ? "All systems operational" : "System needs attention"}</h2>
            <p>{servicesHealthy} of {snapshot.services.length} services healthy · {number(snapshot.metrics.pending_classification)} emails awaiting AI</p>
          </div>
          <span className="monitoring-overview-state">{snapshot.overall_status}</span>
        </section>

        <section className="monitoring-metrics" aria-label="Platform totals">
          {[
            { label: "Active mailboxes", value: snapshot.metrics.active_mailboxes, detail: "Connected provider accounts", icon: <><rect x="3" y="4" width="18" height="16" rx="3" /><path d="M3 14h5l2 3h4l2-3h5" /></> },
            { label: "Stored emails", value: snapshot.metrics.emails, detail: "Available to search and classify", icon: <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 7 9 6 9-6" /></> },
            { label: "Pending AI", value: snapshot.metrics.pending_classification, detail: "Waiting for classification", icon: <><rect x="6" y="6" width="12" height="12" rx="2" /><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2" /></> },
            { label: "Sign-in users", value: snapshot.metrics.users, detail: "Application accounts", icon: <><circle cx="9" cy="8" r="3.2" /><path d="M2.5 20a6.5 6.5 0 0 1 13 0M16.4 11.2A3 3 0 1 0 15 5.5M21.5 20a6 6 0 0 0-3.4-5.4" /></> },
          ].map((metric) => (
            <article className="monitoring-metric" key={metric.label}>
              <span className="monitoring-metric-icon" aria-hidden="true"><svg viewBox="0 0 24 24">{metric.icon}</svg></span>
              <span className="monitoring-metric-label">{metric.label}</span>
              <strong>{number(metric.value)}</strong>
              <small>{metric.detail}</small>
            </article>
          ))}
        </section>

        {view === "architecture" ? (
          <section className="monitoring-section">
            <div className="monitoring-section-title">
              <div><p>Runtime</p><h2>Live pipeline</h2></div>
              <div className="arch-legend" aria-hidden="true">
                <span className={`arch-live-chip${flowing ? " is-active" : ""}`}><span className="arch-live-dot" />{flowing ? "Data flowing" : "Idle"}</span>
                <span><span className="monitor-status-dot status-healthy" />Healthy</span>
                <span><span className="monitor-status-dot status-stale" />Degraded</span>
                <span><span className="monitor-status-dot status-offline" />Offline</span>
              </div>
            </div>

            <div className="arch-diagram" role="list" aria-label="Service pipeline">
              <ArchNode live={liveStage("providers")}
                icon="provider" title="Email providers" subtitle="Gmail · Microsoft Graph" variant="endpoint"
                status={snapshot.mailbox_issues.length ? "stale" : "healthy"}
                statusLabel={snapshot.mailbox_issues.length ? `${snapshot.mailbox_issues.length} issues` : "connected"}
                meta={`${number(snapshot.metrics.active_mailboxes)} mailboxes`}
              />
              <ArchEdge label="webhooks · polling" count={snapshot.queues.pending_webhooks} live={liveEdge("providers", "sync")} />

              <ArchNode live={liveStage("sync")} icon="sync" title="Sync worker" subtitle={svc("sync-worker")?.description ?? "Mailbox ingestion"}
                status={svc("sync-worker")?.status ?? "offline"} meta={`${number(snapshot.queues.active_mailbox_jobs)} jobs · ${nodeMeta(svc("sync-worker"))}`} />
              <ArchEdge label="writes rows" live={liveEdge("sync", "postgres")} />

              <ArchNode live={liveStage("postgres")} icon="store" title="PostgreSQL" subtitle="System of record · transactional outbox" variant="store"
                status={snapshot.infrastructure.postgres.status} meta={`${number(snapshot.metrics.emails)} emails stored`} />
              <ArchEdge label="outbox: email.imported.v1" count={snapshot.queues.pending_events} error={snapshot.queues.failed_events} live={liveEdge("postgres", "relay")} />

              <ArchNode live={liveStage("relay")} icon="relay" title="Event relay" subtitle={svc("event-relay")?.description ?? "Outbox → RabbitMQ"}
                status={svc("event-relay")?.status ?? "offline"} meta={nodeMeta(svc("event-relay"))} />
              <ArchEdge label="publish to exchange" live={liveEdge("relay", "rabbitmq")} />

              <ArchNode live={liveStage("rabbitmq")} icon="broker" title="RabbitMQ" subtitle="Topic exchange · durable task & event delivery" variant="store"
                status={snapshot.infrastructure.rabbitmq.status}
                statusLabel={snapshot.infrastructure.rabbitmq.status === "disabled" ? "disabled" : undefined} />
              <ArchEdge label="email.imported.v1" count={snapshot.metrics.pending_classification} live={liveEdge("rabbitmq", "classification")} />

              <ArchNode live={liveStage("classification")} icon="classify" title="Classification worker" subtitle={svc("classification-worker")?.description ?? "AI classify + calendar"}
                status={svc("classification-worker")?.status ?? "offline"} meta={nodeMeta(svc("classification-worker"))} />
              <ArchEdge label="user.activity.v1" live={liveEdge("classification", "notification")} />

              <ArchNode live={liveStage("notification")} icon="notify" title="Notification service" subtitle={svc("notification-service")?.description ?? "Notification routing"}
                status={svc("notification-service")?.status ?? "offline"} meta={nodeMeta(svc("notification-service"))} />
              <ArchEdge label="notification.ready.v1" live={liveEdge("notification", "api")} />

              <ArchNode live={liveStage("api")} icon="api" title="Email API" subtitle={svc("email-api")?.description ?? "REST · OAuth · WebSockets"}
                status={svc("email-api")?.status ?? "offline"} meta={nodeMeta(svc("email-api"))} />
              <ArchEdge label="SSE · WebSocket" live={liveEdge("api", "browser")} />

              <ArchNode live={liveStage("browser")} icon="client" title="Browser UI" subtitle="Live inbox, calendar & notifications" variant="endpoint"
                status="healthy" statusLabel="streaming" meta="real-time updates" />
            </div>
          </section>
        ) : (
          <>
            <section className="monitoring-section">
              <div className="monitoring-section-title"><div><p>Runtime</p><h2>Application services</h2></div><span>Heartbeat threshold: 45 seconds</span></div>
              <div className="monitoring-services">{snapshot.services.map((service) => <ServiceCard key={service.name} service={service} />)}</div>
            </section>

            <section className="monitoring-split">
              <div className="monitoring-section monitoring-panel">
                <div className="monitoring-section-title"><div><p>Infrastructure</p><h2>Data plane</h2></div></div>
                <div className="monitoring-infra-list">
                  {[snapshot.infrastructure.postgres, snapshot.infrastructure.rabbitmq].map((item) => (
                    <div className="monitoring-infra-row" key={item.label}>
                      <span className="monitoring-infra-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7" /></svg></span>
                      <div><strong>{item.label}</strong><small>{item.label === "RabbitMQ" ? "Task delivery and live event fan-out" : "Durable application state"}</small></div>
                      <span className={`monitor-status status-${item.status}`}><StatusDot status={item.status} />{item.status}</span>
                    </div>
                  ))}
                </div>
              </div>

              <div className="monitoring-section monitoring-panel">
                <div className="monitoring-section-title"><div><p>Processing</p><h2>Queue pressure</h2></div></div>
                <div className="monitoring-queue-grid">
                  <div><span>Mailbox jobs</span><strong>{number(snapshot.queues.active_mailbox_jobs)}</strong><small>queued or running</small></div>
                  <div><span>Webhook jobs</span><strong>{number(snapshot.queues.pending_webhooks)}</strong><small>awaiting ingestion</small></div>
                  <div><span>Outbox events</span><strong>{number(snapshot.queues.pending_events)}</strong><small>awaiting RabbitMQ</small></div>
                  <div className={snapshot.queues.failed_events ? "has-error" : ""}><span>Failed events</span><strong>{number(snapshot.queues.failed_events)}</strong><small>need retry</small></div>
                </div>
              </div>
            </section>
          </>
        )}

        <section className="monitoring-section monitoring-issues">
          <div className="monitoring-section-title"><div><p>Delivery</p><h2>Mailbox issues</h2></div><span>{snapshot.mailbox_issues.length} active issues</span></div>
          {snapshot.mailbox_issues.length === 0 ? (
            <div className="monitoring-empty"><span>✓</span><div><strong>No mailbox failures</strong><p>All connected accounts are free of recorded sync and webhook errors.</p></div></div>
          ) : (
            <div className="monitoring-issue-table" role="table" aria-label="Mailbox issues">
              <div className="monitoring-issue-head" role="row"><span>Account</span><span>Provider</span><span>Last successful sync</span><span>Problem</span></div>
              {snapshot.mailbox_issues.map((issue) => <div className="monitoring-issue-row" role="row" key={issue.mailbox_id}>
                <strong>{issue.email_address}</strong><span className={`monitoring-provider provider-${issue.provider}`}>{issue.provider}</span><span>{relativeTime(issue.last_sync_completed_at)}</span><span title={issue.message}>{issue.message}</span>
              </div>)}
            </div>
          )}
        </section>
      </>}
    </main>
  );
}
