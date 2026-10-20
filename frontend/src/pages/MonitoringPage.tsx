import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchMonitoringSnapshot, type MonitoringService, type MonitoringSnapshot } from "../api";

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

export default function MonitoringPage({ active }: { active: boolean }) {
  const [snapshot, setSnapshot] = useState<MonitoringSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  if (loading && !snapshot) {
    return <main className="monitoring-page"><div className="monitoring-loading"><span className="monitoring-spinner" />Loading system health…</div></main>;
  }

  return (
    <main className="monitoring-page">
      <header className="monitoring-header">
        <div className="monitoring-heading">
          <p className="monitoring-eyebrow">Admin operations</p>
          <h1>System monitoring</h1>
          <p>Live service health, processing queues, and account delivery problems.</p>
        </div>
        <div className="monitoring-header-actions">
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
            <p>{servicesHealthy} of {snapshot.services.length} application services are reporting normally.</p>
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
