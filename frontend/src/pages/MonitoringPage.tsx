import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { fetchMonitoringSnapshot, openEvents, type MonitoringService, type MonitoringSnapshot } from "../api";

type ViewMode = "dashboard" | "architecture";
const VIEW_KEY = "monitoring-view";

// Which pipeline stages light up when a given live event arrives. Keys match
// the `data-stage` of each node below so the diagram shows real data flow.
const FLOW_STAGES = ["providers", "sync", "postgres", "relay", "rabbitmq", "classification", "notification", "api", "browser"] as const;
type Stage = (typeof FLOW_STAGES)[number];
const EVENT_FLOW: Record<string, Stage[]> = {
  "sync.progress": ["providers", "sync", "postgres", "relay", "rabbitmq", "notification", "api", "browser"],
  "sync.done": ["providers", "sync", "postgres", "relay", "rabbitmq", "notification", "api", "browser"],
  "email.classified": ["classification", "postgres", "relay", "rabbitmq", "notification", "api", "browser"],
  "reclassify.progress": ["classification", "postgres", "relay", "rabbitmq", "notification", "api", "browser"],
  "reclassify.done": ["classification", "postgres", "relay", "rabbitmq", "notification", "api", "browser"],
  "calendar.changed": ["classification", "postgres", "relay", "rabbitmq", "notification", "api", "browser"],
};
const EVENT_LABELS: Record<string, string> = {
  "sync.progress": "Mailbox sync progressing",
  "sync.done": "Mailbox sync completed",
  "email.classified": "Email classified",
  "reclassify.progress": "AI reclassification progressing",
  "reclassify.done": "AI reclassification completed",
  "calendar.changed": "Interview calendar updated",
};
const FLOW_STEP_MS = 135;
const FLOW_ACTIVE_MS = 620;

type FlowWindow = { start: number; end: number };
type StreamState = "connecting" | "connected" | "reconnecting";
type LiveActivity = { label: string; detail: string; receivedAt: number };

function eventDetail(eventName: string, data: Record<string, unknown>): string {
  const mailbox = typeof data.mailbox_id === "number" ? `Mailbox #${data.mailbox_id}` : "Live system event";
  const total = typeof data.total === "number" ? data.total : null;
  if (eventName === "sync.progress") {
    const imported = typeof data.imported === "number" ? data.imported : 0;
    return total ? `${mailbox} · ${number(imported)} of ${number(total)} messages` : `${mailbox} · ${number(imported)} imported`;
  }
  if (eventName === "reclassify.progress") {
    const processed = typeof data.processed === "number" ? data.processed : 0;
    return total ? `${mailbox} · ${number(processed)} of ${number(total)} processed` : `${mailbox} · processing`;
  }
  if (eventName === "sync.done") {
    const imported = typeof data.imported === "number" ? data.imported : null;
    return imported == null ? `${mailbox} · ${String(data.state ?? "complete")}` : `${mailbox} · ${number(imported)} imported`;
  }
  if (eventName === "reclassify.done") {
    const updated = typeof data.updated === "number" ? data.updated : null;
    return updated == null ? `${mailbox} · ${String(data.state ?? "complete")}` : `${mailbox} · ${number(updated)} updated`;
  }
  if (eventName === "email.classified") {
    return typeof data.label === "string" ? `${mailbox} · ${data.label}` : mailbox;
  }
  if (eventName === "calendar.changed") {
    return typeof data.action === "string" ? `${mailbox} · ${data.action.replaceAll("_", " ")}` : mailbox;
  }
  return mailbox;
}

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

type ConnectorDirection = "down" | "up" | "right" | "left" | "vertical-both" | "horizontal-both";

function ArchConnector({
  label, detail, count, error, live, direction = "down", className = "",
}: {
  label: string;
  detail?: string;
  count?: number;
  error?: number;
  live?: boolean;
  direction?: ConnectorDirection;
  className?: string;
}) {
  const vertical = direction === "down" || direction === "up" || direction === "vertical-both";
  return (
    <div className={`arch-connector is-${direction}${vertical ? " is-vertical" : " is-horizontal"}${live ? " is-live" : ""} ${className}`} aria-hidden="true">
      <svg viewBox={vertical ? "0 0 24 100" : "0 0 100 24"} preserveAspectRatio="none">
        <path className="arch-connector-line" d={vertical ? "M12 4V96" : "M4 12H96"} vectorEffect="non-scaling-stroke" />
        {(direction === "down" || direction === "vertical-both") && <path className="arch-connector-arrow" d="M7 88l5 8 5-8" vectorEffect="non-scaling-stroke" />}
        {(direction === "up" || direction === "vertical-both") && <path className="arch-connector-arrow" d="M7 12l5-8 5 8" vectorEffect="non-scaling-stroke" />}
        {(direction === "right" || direction === "horizontal-both") && <path className="arch-connector-arrow" d="M88 7l8 5-8 5" vectorEffect="non-scaling-stroke" />}
        {(direction === "left" || direction === "horizontal-both") && <path className="arch-connector-arrow" d="M12 7l-8 5 8 5" vectorEffect="non-scaling-stroke" />}
      </svg>
      {live && <span className="arch-packets"><i /><i /></span>}
      <span className="arch-connector-chip">
        <strong>{label}</strong>
        {detail && <small>{detail}</small>}
        {typeof count === "number" && count > 0 && <b className="arch-edge-badge">{number(count)}</b>}
        {typeof error === "number" && error > 0 && <b className="arch-edge-badge is-error">{number(error)} failed</b>}
      </span>
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
  const pulsesRef = useRef<Partial<Record<Stage, FlowWindow>>>({});
  const [, setFlowTick] = useState(0);
  const [streamState, setStreamState] = useState<StreamState>("connecting");
  const [lastActivity, setLastActivity] = useState<LiveActivity | null>(null);
  const [liveEventCount, setLiveEventCount] = useState(0);
  useEffect(() => {
    if (!active || view !== "architecture") return;
    const stream = openEvents();
    const handlers: Array<[string, EventListener]> = [];
    setStreamState("connecting");
    const bump = (eventName: string, stages: Stage[], event: Event) => {
      const now = Date.now();
      let data: Record<string, unknown> = {};
      try {
        const parsed = JSON.parse((event as MessageEvent).data || "{}") as unknown;
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) data = parsed as Record<string, unknown>;
      } catch {
        // The animation still represents the event even if optional telemetry is malformed.
      }
      pulsesRef.current = {};
      stages.forEach((stage, index) => {
        const start = now + index * FLOW_STEP_MS;
        pulsesRef.current[stage] = { start, end: start + FLOW_ACTIVE_MS };
      });
      setLastActivity({ label: EVENT_LABELS[eventName] ?? eventName, detail: eventDetail(eventName, data), receivedAt: now });
      setLiveEventCount((count) => count + 1);
      setFlowTick((tick) => tick + 1);
    };
    for (const [eventName, stages] of Object.entries(EVENT_FLOW)) {
      const handler: EventListener = (event) => bump(eventName, stages, event);
      stream.addEventListener(eventName, handler);
      handlers.push([eventName, handler]);
    }
    const onConnected: EventListener = () => setStreamState("connected");
    const onReconnecting: EventListener = () => setStreamState("reconnecting");
    stream.addEventListener("connected", onConnected);
    stream.addEventListener("reconnecting", onReconnecting);
    handlers.push(["connected", onConnected], ["reconnecting", onReconnecting]);
    const ticker = window.setInterval(() => {
      const now = Date.now();
      const windows = Object.entries(pulsesRef.current) as Array<[Stage, FlowWindow]>;
      if (!windows.length) return;
      let activeWindow = false;
      for (const [stage, window] of windows) {
        if (window.end <= now) delete pulsesRef.current[stage];
        else activeWindow = true;
      }
      if (activeWindow || windows.length) setFlowTick((tick) => tick + 1);
    }, 90);
    return () => {
      handlers.forEach(([name, handler]) => stream.removeEventListener(name, handler));
      stream.close();
      window.clearInterval(ticker);
    };
  }, [active, view]);

  const flowNow = Date.now();
  const liveStage = (stage: Stage): boolean => {
    const window = pulsesRef.current[stage];
    return Boolean(window && window.start <= flowNow && window.end > flowNow);
  };
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
                <span className={`arch-live-chip${flowing ? " is-active" : ""}`}><span className="arch-live-dot" />{flowing ? "Event in flight" : "Listening"}</span>
                <span><span className="monitor-status-dot status-healthy" />Healthy</span>
                <span><span className="monitor-status-dot status-stale" />Degraded</span>
                <span><span className="monitor-status-dot status-offline" />Offline</span>
              </div>
            </div>

            <div className={`arch-telemetry is-${streamState}`} aria-live="polite">
              <div className="arch-stream-signal" aria-hidden="true"><i /><i /><i /><i /></div>
              <div className="arch-stream-copy">
                <span>Live event stream</span>
                <strong>{streamState === "connected" ? "WebSocket connected" : streamState === "reconnecting" ? "Reconnecting automatically" : "Connecting to activity stream"}</strong>
              </div>
              <div className="arch-last-event">
                <span>{lastActivity ? lastActivity.label : "Awaiting the next application event"}</span>
                <strong>{lastActivity ? lastActivity.detail : "The route will animate when live data arrives"}</strong>
              </div>
              <div className="arch-live-stat"><span>Events this session</span><strong>{number(liveEventCount)}</strong></div>
              <div className="arch-live-stat"><span>Last event</span><strong>{lastActivity ? relativeTime(new Date(lastActivity.receivedAt).toISOString()) : "—"}</strong></div>
            </div>

            <div className="arch-diagram" role="list" aria-label="System service topology">
              <div className="arch-zone-label arch-zone-external"><span>External systems</span></div>
              <div className="arch-zone-label arch-zone-data"><span>Durable data plane</span></div>
              <div className="arch-zone-label arch-zone-client"><span>Client channel</span></div>

              <div className="arch-slot arch-slot-providers">
                <ArchNode live={liveStage("providers")}
                  icon="provider" title="Email providers" subtitle="Gmail API · Microsoft Graph" variant="endpoint"
                  status={snapshot.mailbox_issues.length ? "stale" : "healthy"}
                  statusLabel={snapshot.mailbox_issues.length ? `${snapshot.mailbox_issues.length} issues` : "connected"}
                  meta={`${number(snapshot.metrics.active_mailboxes)} mailboxes`}
                />
              </div>
              <ArchConnector className="arch-route-provider" label="Inbound mail" detail="webhook + safety polling" count={snapshot.queues.pending_webhooks} live={liveEdge("providers", "sync")} />
              <div className="arch-slot arch-slot-browser">
                <ArchNode live={liveStage("browser")} icon="client" title="Browser UI" subtitle="Inbox · calendar · in-app alerts" variant="endpoint"
                  status="healthy" statusLabel="connected" meta="reconnectable event stream" />
              </div>
              <ArchConnector className="arch-route-browser" label="HTTPS" detail="REST ↓ · WebSocket ↑" direction="vertical-both" live={liveEdge("api", "browser")} />

              <div className="arch-slot arch-slot-sync">
                <ArchNode live={liveStage("sync")} icon="sync" title="Sync worker" subtitle="Webhook and polling mailbox ingestion"
                  status={svc("sync-worker")?.status ?? "offline"} meta={`${number(snapshot.queues.active_mailbox_jobs)} jobs · ${nodeMeta(svc("sync-worker"))}`} />
              </div>
              <ArchConnector className="arch-route-sync-db" label="Commit" detail="emails + job state" direction="right" live={liveEdge("sync", "postgres")} />
              <div className="arch-slot arch-slot-postgres">
                <ArchNode live={liveStage("postgres")} icon="store" title="PostgreSQL" subtitle="Mail · jobs · activity log · transactional outbox" variant="store"
                  status={snapshot.infrastructure.postgres.status} meta={`${number(snapshot.metrics.emails)} emails stored`} />
              </div>
              <ArchConnector className="arch-route-db-api" label="Application data" detail="queries ↔ commands" direction="horizontal-both" live={liveEdge("postgres", "api")} />
              <div className="arch-slot arch-slot-api">
                <ArchNode live={liveStage("api")} icon="api" title="Email API" subtitle={svc("email-api")?.description ?? "REST · OAuth · webhooks · WebSocket events"}
                  status={svc("email-api")?.status ?? "offline"} meta={nodeMeta(svc("email-api"))} />
              </div>

              <ArchConnector className="arch-route-outbox" label="Transactional outbox" detail="claim pending events" count={snapshot.queues.pending_events} error={snapshot.queues.failed_events} live={liveEdge("postgres", "relay")} />
              <div className="arch-slot arch-slot-relay">
                <ArchNode live={liveStage("relay")} icon="relay" title="Event relay" subtitle={svc("event-relay")?.description ?? "Outbox publisher with confirms"}
                  status={svc("event-relay")?.status ?? "offline"} meta={nodeMeta(svc("event-relay"))} />
              </div>
              <ArchConnector className="arch-route-publish" label="Topic publish" detail="publisher confirms" live={liveEdge("relay", "rabbitmq")} />

              <div className="arch-zone-label arch-zone-worker"><span>Async consumers</span></div>
              <div className="arch-zone-label arch-zone-broker"><span>Event backbone</span></div>
              <div className="arch-zone-label arch-zone-delivery"><span>Notification delivery</span></div>

              <div className="arch-slot arch-slot-classification">
                <ArchNode live={liveStage("classification")} icon="classify" title="Classification worker" subtitle="Consumes imports · writes AI labels and calendar"
                  status={svc("classification-worker")?.status ?? "offline"} meta={nodeMeta(svc("classification-worker"))} />
              </div>
              <ArchConnector className="arch-route-classify" label="email.imported.v1" detail="durable work queue" direction="left" count={snapshot.metrics.pending_classification} live={liveEdge("rabbitmq", "classification")} />
              <div className="arch-slot arch-slot-rabbitmq">
                <ArchNode live={liveStage("rabbitmq")} icon="broker" title="RabbitMQ" subtitle="Topic exchange · durable queues · acknowledgements" variant="store"
                  status={snapshot.infrastructure.rabbitmq.status}
                  statusLabel={snapshot.infrastructure.rabbitmq.status === "disabled" ? "disabled" : undefined}
                  meta={snapshot.infrastructure.rabbitmq.status === "disabled" ? "database fallback active" : "aec.events exchange"} />
              </div>
              <ArchConnector className="arch-route-notify" label="user.activity.v1" detail="durable consumer" direction="right" live={liveEdge("rabbitmq", "notification")} />
              <div className="arch-slot arch-slot-notification">
                <ArchNode live={liveStage("notification")} icon="notify" title="Notification service" subtitle="Persists in-app notifications · emits delivery events"
                  status={svc("notification-service")?.status ?? "offline"} meta={nodeMeta(svc("notification-service"))} />
              </div>
              <ArchConnector className="arch-route-ready" label="notification.ready.v1" detail="outbox → broker → API" direction="up" live={liveEdge("notification", "api")} />
            </div>

            <div className="arch-contracts" aria-label="Runtime message flows">
              <article><span>01</span><div><strong>Ingest</strong><p>Provider → sync worker → PostgreSQL</p></div></article>
              <article><span>02</span><div><strong>Classify</strong><p>Outbox → RabbitMQ → AI worker → PostgreSQL</p></div></article>
              <article><span>03</span><div><strong>Notify</strong><p>Activity event → notification service → API → WebSocket</p></div></article>
              <article><span>04</span><div><strong>Recover</strong><p>PostgreSQL remains authoritative during broker or browser reconnects</p></div></article>
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
