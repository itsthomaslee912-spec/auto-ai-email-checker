import { useEffect, useRef, type ReactNode } from "react";

export type SystemIssueAction = {
  label: string;
  onClick: () => void;
  primary?: boolean;
};

export type SystemIssue = {
  id: string;
  kind: "sync" | "ai" | "service";
  severity: "error" | "warning";
  title: string;
  detail: string;
  items?: { id: string; label: string; note?: string; action?: SystemIssueAction }[];
  actions?: SystemIssueAction[];
};

const KIND_ICONS: Record<SystemIssue["kind"], ReactNode> = {
  sync: <><rect x="3" y="5" width="18" height="14" rx="3" /><path d="m4 7 8 6 8-6" /></>,
  ai: <><path d="M11 3.5l1.7 4.4 4.4 1.7-4.4 1.7L11 15.7l-1.7-4.4-4.4-1.7 4.4-1.7z" /><path d="M18 14.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z" /></>,
  service: <><rect x="4" y="4" width="16" height="6" rx="2" /><rect x="4" y="14" width="16" height="6" rx="2" /><path d="M8 7h.01M8 17h.01" /></>,
};

function Icon({ children, className }: { children: ReactNode; className?: string }) {
  return <svg className={className} viewBox="0 0 24 24" aria-hidden="true">{children}</svg>;
}

function headline(issues: SystemIssue[]): { title: string; subtitle: string } {
  if (issues.length === 1) {
    const issue = issues[0];
    const area = issue.kind === "ai" ? "AI model" : issue.kind === "sync" ? "Email sync" : "App service";
    return { title: `${area} needs attention`, subtitle: "Fix the problem below to get everything working again." };
  }
  return {
    title: `${issues.length} problems need attention`,
    subtitle: "Some parts of the app are not working right now.",
  };
}

export default function SystemAlert({
  issues,
  open,
  checking,
  onDismiss,
  onReopen,
  onRecheck,
}: {
  issues: SystemIssue[];
  open: boolean;
  checking: boolean;
  onDismiss: () => void;
  onReopen: () => void;
  onRecheck: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const dialog = dialogRef.current;
    (dialog?.querySelector<HTMLButtonElement>("button.is-primary") ?? dialog?.querySelector<HTMLButtonElement>("button"))?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onDismiss();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onDismiss]);

  if (!issues.length) return null;
  const tone = issues.some((issue) => issue.severity === "error") ? "is-error" : "is-warning";
  const { title, subtitle } = headline(issues);

  if (!open) {
    return (
      <button
        type="button"
        className={`system-alert-pill ${tone}`}
        onClick={onReopen}
        aria-label={`${title}. Show details`}
      >
        <span className="system-alert-pill-dot" aria-hidden="true" />
        <span>{issues.length === 1 ? issues[0].title : title}</span>
        <strong>View</strong>
      </button>
    );
  }

  return (
    <div className="system-alert-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onDismiss()}>
      <div
        ref={dialogRef}
        className={`system-alert ${tone}`}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="system-alert-title"
        aria-describedby="system-alert-subtitle"
      >
        <header className="system-alert-header">
          <div className="system-alert-badge" aria-hidden="true">
            <Icon><><path d="M12 4 2.8 19.5h18.4z" /><path d="M12 10v4.2M12 17.2h.01" /></></Icon>
          </div>
          <div>
            <h2 id="system-alert-title">{title}</h2>
            <p id="system-alert-subtitle">{subtitle}</p>
          </div>
          <button type="button" className="system-alert-close" aria-label="Close" onClick={onDismiss}>
            <Icon><path d="M6 6l12 12M18 6 6 18" /></Icon>
          </button>
        </header>

        <div className="system-alert-body">
          {issues.map((issue) => (
            <section className={`system-alert-issue is-${issue.severity}`} key={issue.id}>
              <div className="system-alert-issue-icon">
                <Icon>{KIND_ICONS[issue.kind]}</Icon>
              </div>
              <div className="system-alert-issue-main">
                <h3>{issue.title}</h3>
                <p>{issue.detail}</p>
                {issue.items && issue.items.length > 0 && (
                  <ul className="system-alert-items">
                    {issue.items.map((item) => (
                      <li key={item.id}>
                        <div>
                          <strong>{item.label}</strong>
                          {item.note && <span>{item.note}</span>}
                        </div>
                        {item.action && (
                          <button type="button" className="system-alert-btn small is-primary" onClick={item.action.onClick}>
                            {item.action.label}
                          </button>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                {issue.actions && issue.actions.length > 0 && (
                  <div className="system-alert-issue-actions">
                    {issue.actions.map((action) => (
                      <button
                        type="button"
                        key={action.label}
                        className={`system-alert-btn small${action.primary ? " is-primary" : ""}`}
                        onClick={action.onClick}
                      >
                        {action.label}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </section>
          ))}
        </div>

        <footer className="system-alert-footer">
          <button type="button" className="system-alert-btn" onClick={onRecheck} disabled={checking}>
            <Icon className={checking ? "is-spinning" : undefined}><><path d="M20 7v5h-5M4 17v-5h5" /><path d="M6.1 8.2A7 7 0 0 1 18.8 7L20 12M4 12l1.2 5A7 7 0 0 0 17.9 15.8" /></></Icon>
            {checking ? "Checking…" : "Check again"}
          </button>
          <button type="button" className="system-alert-btn ghost" onClick={onDismiss}>Remind me later</button>
        </footer>
      </div>
    </div>
  );
}
