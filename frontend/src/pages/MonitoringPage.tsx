import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { fetchMonitoringSnapshot, openEvents, type AiStatus, type MonitoringService, type MonitoringSnapshot } from "../api";

type ViewMode = "dashboard" | "architecture";
const VIEW_KEY = "monitoring-view";

// Which pipeline stages light up when a given live event arrives. Keys match
// the `data-stage` of each node below so the diagram shows real data flow.
const FLOW_STAGES = ["providers", "sync", "postgres", "relay", "rabbitmq", "classification", "model", "notification", "api", "browser"] as const;
type Stage = (typeof FLOW_STAGES)[number];
const EVENT_FLOW: Record<string, Stage[]> = {
  "sync.progress": ["providers", "sync", "postgres", "relay", "rabbitmq", "notification", "api", "browser"],
  "sync.done": ["providers", "sync", "postgres", "relay", "rabbitmq", "notification", "api", "browser"],
  "email.classified": ["classification", "model", "postgres", "relay", "rabbitmq", "notification", "api", "browser"],
  "reclassify.progress": ["classification", "model", "postgres", "relay", "rabbitmq", "notification", "api", "browser"],
  "reclassify.done": ["classification", "model", "postgres", "relay", "rabbitmq", "notification", "api", "browser"],
  "calendar.changed": ["classification", "model", "postgres", "relay", "rabbitmq", "notification", "api", "browser"],
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
type ClassificationActivity = { processed: number; total: number; updated: number; failed: number };

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

const STATUS_LABELS: Record<string, string> = { recovering: "reconnecting" };
function statusText(status: string): string {
  return STATUS_LABELS[status] ?? status;
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
        <span className="monitor-status"><StatusDot status={service.status} />{statusText(service.status)}</span>
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
  model: <><rect x="5" y="5" width="14" height="14" rx="3" /><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3" /><circle cx="12" cy="12" r="3" /></>,
  notify: <><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.7 21a2 2 0 0 1-3.4 0" /></>,
  api: <><rect x="3" y="4" width="18" height="16" rx="3" /><path d="M8 9l-2 3 2 3M16 9l2 3-2 3" /></>,
  client: <><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M8 20h8M12 16v4" /></>,
  store: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7" /></>,
};
const ICON_CODES: Record<string, string> = {
  provider: "MX", sync: "SY", relay: "ER", broker: "MQ", classify: "AI",
  model: "LLM", notify: "NT", api: "API", client: "UI", store: "DB",
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
    <article className={`arch-node status-${status}${variant ? ` is-${variant}` : ""}${live ? " is-live" : ""}`}>
      <span className="arch-node-icon" data-code={ICON_CODES[icon] ?? "::"} aria-hidden="true"><Icon name={icon} /></span>
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

function ArchitectureConnector({
  className, label, detail, direction = "down", live, badge, badgeTone,
}: {
  className: string;
  label: string;
  detail: string;
  direction?: "right" | "left" | "down" | "up" | "both-horizontal" | "both-vertical";
  live?: boolean;
  badge?: string;
  badgeTone?: "error" | "warning";
}) {
  const horizontal = direction === "right" || direction === "left" || direction === "both-horizontal";
  const reverse = direction === "left" || direction === "up";
  const bidirectional = direction === "both-horizontal" || direction === "both-vertical";
  return (
    <div
      className={`arch-connector ${horizontal ? "is-horizontal" : "is-vertical"}${reverse ? horizontal ? " is-left" : " is-up" : ""}${bidirectional ? horizontal ? " is-horizontal-both" : " is-vertical-both" : ""}${live ? " is-live" : ""} ${className}`}
      aria-label={`${label}: ${detail}`}
    >
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
        {horizontal ? (
          <>
            <path className="arch-connector-line" d="M4 50 H96" />
            {(!reverse || bidirectional) && <path d="M90 43 L96 50 L90 57" />}
            {(reverse || bidirectional) && <path d="M10 43 L4 50 L10 57" />}
          </>
        ) : (
          <>
            <path className="arch-connector-line" d="M50 4 V96" />
            {(!reverse || bidirectional) && <path d="M43 90 L50 96 L57 90" />}
            {(reverse || bidirectional) && <path d="M43 10 L50 4 L57 10" />}
          </>
        )}
      </svg>
      {live && <span className="arch-packets" aria-hidden="true"><i /><i /></span>}
      <span className="arch-connector-chip">
        <strong>{label}</strong>
        <small>{detail}</small>
        {badge != null && <em className={`arch-edge-badge${badgeTone ? ` is-${badgeTone}` : ""}`}>{badge}</em>}
      </span>
    </div>
  );
}

export default function MonitoringPage({ active, aiStatus, aiStatusOffline }: { active: boolean; aiStatus: AiStatus | null; aiStatusOffline: boolean }) {
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
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load(true);
    }, 30_000);
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
  const aiProvider = aiStatus?.provider === "openai"
    ? "OpenAI"
    : aiStatus?.provider === "gemini"
      ? "Google Gemini"
      : aiStatus?.provider === "ollama"
        ? "Ollama"
        : "AI provider";
  const aiModelState = aiStatusOffline ? "offline" : !aiStatus ? "stale" : aiStatus.ready ? "healthy" : "degraded";
  const aiModelStateLabel = aiStatusOffline ? "unreachable" : !aiStatus ? "checking" : aiStatus.ready ? "ready" : "not ready";

  // Live data flow: subscribe to the shared event stream and briefly light up
  // the pipeline stages each event passes through, so the diagram animates
  // actual traffic (a synced email, a classification, a calendar update).
  const pulsesRef = useRef<Partial<Record<Stage, FlowWindow>>>({});
  const snapshotRefreshTimerRef = useRef<number | null>(null);
  const [, setFlowTick] = useState(0);
  const [streamState, setStreamState] = useState<StreamState>("connecting");
  const [lastActivity, setLastActivity] = useState<LiveActivity | null>(null);
  const [liveEventCount, setLiveEventCount] = useState(0);
  const [liveClassificationCount, setLiveClassificationCount] = useState(0);
  const [classificationActivity, setClassificationActivity] = useState<ClassificationActivity | null>(null);
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
      if (eventName === "email.classified") setLiveClassificationCount((count) => count + 1);
      if (eventName === "reclassify.progress") {
        setClassificationActivity({
          processed: typeof data.processed === "number" ? data.processed : 0,
          total: typeof data.total === "number" ? data.total : 0,
          updated: typeof data.updated === "number" ? data.updated : 0,
          failed: typeof data.failed === "number" ? data.failed : 0,
        });
      } else if (eventName === "reclassify.done") {
        setClassificationActivity(null);
      }
      if (["email.classified", "sync.done", "reclassify.progress", "reclassify.done"].includes(eventName)) {
        if (snapshotRefreshTimerRef.current != null) window.clearTimeout(snapshotRefreshTimerRef.current);
        // Coalesce classify storms so the heavy monitoring snapshot is not
        // rebuilt on every single email.classified event.
        snapshotRefreshTimerRef.current = window.setTimeout(() => {
          snapshotRefreshTimerRef.current = null;
          void load(true);
        }, eventName === "email.classified" || eventName === "reclassify.progress" ? 8_000 : 1_000);
      }
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
      if (snapshotRefreshTimerRef.current != null) window.clearTimeout(snapshotRefreshTimerRef.current);
      snapshotRefreshTimerRef.current = null;
    };
  }, [active, view, load]);

  const flowNow = Date.now();
  const liveStage = (stage: Stage): boolean => {
    const window = pulsesRef.current[stage];
    return Boolean(window && window.start <= flowNow && window.end > flowNow);
  };
  const liveEdge = (from: Stage, to: Stage): boolean => liveStage(from) || liveStage(to);
  const flowing = FLOW_STAGES.some(liveStage);
  const classifiedEmails = snapshot
    ? snapshot.metrics.classified_emails ?? Math.max(0, snapshot.metrics.emails - snapshot.metrics.pending_classification)
    : 0;
  const humanFeedback = snapshot?.metrics.human_feedback ?? 0;

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

            <section className="arch-blueprint-metrics arch-runtime-metrics" aria-label="Live architecture metrics">
                <article><span>Stored emails</span><strong>{number(snapshot.metrics.emails)}</strong><small>Authoritative records</small></article>
                <article><span>Classified</span><strong>{number(classifiedEmails)}</strong><small>{snapshot.metrics.emails ? `${Math.round(classifiedEmails / snapshot.metrics.emails * 100)}% complete` : "No stored mail"}</small></article>
                <article className={snapshot.metrics.pending_classification ? "has-pressure" : ""}><span>Pending AI</span><strong>{number(snapshot.metrics.pending_classification)}</strong><small>Queued or processing</small></article>
                <article><span>Human feedback</span><strong>{number(humanFeedback)}</strong><small>Saved corrections</small></article>
                <article><span>Live classifications</span><strong>{number(liveClassificationCount)}</strong><small>This browser session</small></article>
                <article><span>Active jobs</span><strong>{number(snapshot.queues.active_mailbox_jobs)}</strong><small>Mailbox operations</small></article>
            </section>

            <div className="arch-diagram" role="group" aria-label="Live system architecture and data flow">
              <div className="arch-zone-label arch-zone-external"><span>External integrations</span></div>
              <div className="arch-zone-label arch-zone-data"><span>Durable platform core</span></div>
              <div className="arch-zone-label arch-zone-client"><span>Client experience</span></div>
              <div className="arch-zone-label arch-zone-worker"><span>Async compute</span></div>
              <div className="arch-zone-label arch-zone-broker"><span>Event backbone</span></div>
              <div className="arch-zone-label arch-zone-delivery"><span>Notification delivery</span></div>

              <div className="arch-slot arch-slot-providers">
                  <ArchNode live={liveStage("providers")} icon="provider" title="Email providers" subtitle="Gmail API · Microsoft Graph"
                    status={snapshot.mailbox_issues.length ? "stale" : "healthy"}
                    statusLabel={snapshot.mailbox_issues.length ? `${snapshot.mailbox_issues.length} issues` : "connected"}
                    meta={`${number(snapshot.metrics.active_mailboxes)} mailboxes`} />
              </div>

              <ArchitectureConnector className="arch-route-provider" label="Inbound mail" detail="webhook + safety polling" direction="down" live={liveEdge("providers", "sync")} badge={`${number(snapshot.queues.pending_webhooks)} pending`} badgeTone={snapshot.queues.pending_webhooks ? "warning" : undefined} />

              <div className="arch-slot arch-slot-sync">
                  <ArchNode live={liveStage("sync")} icon="sync" title="Sync worker" subtitle="Webhook and polling ingestion"
                    status={svc("sync-worker")?.status ?? "offline"} meta={`${number(snapshot.queues.active_mailbox_jobs)} jobs · ${nodeMeta(svc("sync-worker"))}`} />
              </div>

              <ArchitectureConnector className="arch-route-sync-db" label="Atomic commit" detail="email + job state + outbox" direction="right" live={liveEdge("sync", "postgres")} badge={`${number(snapshot.queues.active_mailbox_jobs)} jobs`} />

              <div className="arch-slot arch-slot-postgres">
                <ArchNode live={liveStage("postgres")} icon="store" title="PostgreSQL" subtitle="System of record · transactional outbox"
                  status={snapshot.infrastructure.postgres.status} meta={`${number(snapshot.metrics.emails)} emails · ${number(humanFeedback)} feedback`} variant="store" />
              </div>

              <ArchitectureConnector className="arch-route-db-api" label="Application data" detail="queries + commands" direction="both-horizontal" live={liveEdge("postgres", "api")} badge={number(snapshot.metrics.emails)} />

              <div className="arch-slot arch-slot-api">
                <ArchNode live={liveStage("api")} icon="api" title="Email API" subtitle="REST · OAuth · WebSocket gateway"
                  status={svc("email-api")?.status ?? "offline"} meta={nodeMeta(svc("email-api"))} />
              </div>

              <ArchitectureConnector className="arch-route-outbox" label="Transactional outbox" detail="claim pending events" direction="down" live={liveEdge("postgres", "relay")} badge={`${number(snapshot.queues.pending_events)} pending`} badgeTone={snapshot.queues.pending_events ? "warning" : undefined} />

              <div className="arch-slot arch-slot-relay">
                <ArchNode live={liveStage("relay")} icon="relay" title="Event relay" subtitle="Claims and publishes committed events"
                  status={svc("event-relay")?.status ?? "offline"} meta={nodeMeta(svc("event-relay"))} />
              </div>

              <ArchitectureConnector className="arch-route-publish" label="Topic publish" detail="publisher confirms" direction="down" live={liveEdge("relay", "rabbitmq")} badge={snapshot.queues.failed_events ? `${number(snapshot.queues.failed_events)} failed` : "confirmed"} badgeTone={snapshot.queues.failed_events ? "error" : undefined} />

              <div className="arch-slot arch-slot-rabbitmq">
                <ArchNode live={liveStage("rabbitmq")} icon="broker" title="RabbitMQ" subtitle="Topic exchange · durable queues"
                  status={snapshot.infrastructure.rabbitmq.status}
                  statusLabel={snapshot.infrastructure.rabbitmq.status === "disabled" ? "fallback" : undefined}
                  meta={snapshot.infrastructure.rabbitmq.status === "disabled" ? "PostgreSQL fallback" : "aec.events exchange"} />
              </div>

              <ArchitectureConnector className="arch-route-classify" label="emailImported.v1" detail="durable work queue" direction="left" live={liveEdge("rabbitmq", "classification")} badge={`${number(snapshot.metrics.pending_classification)} pending`} badgeTone={snapshot.metrics.pending_classification ? "warning" : undefined} />

              <div className="arch-slot arch-slot-classification">
                  <ArchNode live={liveStage("classification")} icon="classify" title="Classification worker" subtitle="AI labels and calendar projection"
                    status={svc("classification-worker")?.status ?? "offline"}
                    statusLabel={statusText(svc("classification-worker")?.status ?? "offline")}
                    meta={classificationActivity
                      ? `${number(classificationActivity.processed)}/${number(classificationActivity.total)} processed · ${number(classificationActivity.updated)} updated${classificationActivity.failed ? ` · ${number(classificationActivity.failed)} failed` : ""}`
                      : `${number(snapshot.metrics.pending_classification)} pending · ${nodeMeta(svc("classification-worker"))}`} />
              </div>

              <ArchitectureConnector className="arch-route-model" label="AI inference" detail="prompt ↔ structured result" direction="up" live={liveEdge("classification", "model")} />

              <div className="arch-slot arch-slot-model">
                <ArchNode live={liveStage("model")} icon="model" title="AI provider" subtitle={`${aiProvider} · ${aiStatus?.model ?? "Checking model"}`}
                  status={aiModelState} statusLabel={aiModelStateLabel}
                  meta={aiStatusOffline ? "Status unavailable" : aiStatus?.ready ? "Inference ready" : aiStatus?.reason ?? "Checking readiness"} />
              </div>

              <ArchitectureConnector className="arch-route-notify" label="user.activity.v1" detail="durable consumer" direction="right" live={liveEdge("rabbitmq", "notification")} />

              <div className="arch-slot arch-slot-notification">
                <ArchNode live={liveStage("notification")} icon="notify" title="Notification service" subtitle="Durable in-app alert delivery"
                  status={svc("notification-service")?.status ?? "offline"} meta={nodeMeta(svc("notification-service"))} />
              </div>

              <ArchitectureConnector className="arch-route-ready" label="notification.ready.v1" detail="outbox → broker → API" direction="up" live={liveEdge("notification", "api")} />

              <ArchitectureConnector className="arch-route-browser" label="HTTPS + WebSocket" detail="commands ↔ live updates" direction="both-vertical" live={liveEdge("api", "browser")} />

              <div className="arch-slot arch-slot-browser">
                <ArchNode live={liveStage("browser")} icon="client" title="Browser application" subtitle="Inbox · calendar · monitoring"
                  status={streamState === "connected" ? "healthy" : "recovering"}
                  statusLabel={streamState === "connected" ? "connected" : streamState}
                  meta={`${number(snapshot.metrics.users)} user · ${number(liveEventCount)} live events`} variant="endpoint" />
              </div>
            </div>

            <section className="arch-contracts" aria-label="Architecture guarantees">
              <article><span>01</span><div><strong>At-least-once ingestion</strong><p>Provider webhooks are backed by safety polling; idempotent commits prevent duplicate email records.</p></div></article>
              <article><span>02</span><div><strong>Transactional delivery</strong><p>Email state and outbox events commit together before the relay publishes durable messages.</p></div></article>
              <article><span>03</span><div><strong>Human-in-the-loop AI</strong><p>The classifier combines the selected model with saved corrections and projects interview calendar data.</p></div></article>
              <article><span>04</span><div><strong>Recoverable live updates</strong><p>PostgreSQL stays authoritative while workers, the broker, model, or browser reconnect independently.</p></div></article>
            </section>

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
