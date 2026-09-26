import type { EmailLabel } from "../api";
import { CLASSIFY_LABELS, CLASSIFY_LABEL_TITLES, LABEL_BAR_COLORS } from "../labels";

export default function LabelBarChart({
  counts,
  total,
}: {
  counts: Record<string, number>;
  total: number;
}) {
  const max = Math.max(1, ...CLASSIFY_LABELS.map((key) => counts[key] ?? 0));

  return (
    <div className="stats-chart-wrap stats-card">
      <div className="stats-bars" role="img" aria-label="Category counts">
        {CLASSIFY_LABELS.map((key) => {
          const value = counts[key] ?? 0;
          const pct = (value / max) * 100;
          return (
            <div key={key} className="stats-bar-col" title={`${CLASSIFY_LABEL_TITLES[key]}: ${value}`}>
              <span className="stats-bar-value">{value}</span>
              <div className="stats-bar-track">
                <div
                  className="stats-bar-fill"
                  style={{
                    height: `${Math.max(pct, value > 0 ? 4 : 0)}%`,
                    background: `linear-gradient(180deg, ${LABEL_BAR_COLORS[key as EmailLabel]} 0%, color-mix(in srgb, ${LABEL_BAR_COLORS[key as EmailLabel]} 42%, transparent) 100%)`,
                  }}
                />
              </div>
              <span className="stats-bar-name">{CLASSIFY_LABEL_TITLES[key]}</span>
            </div>
          );
        })}
      </div>
      <p className="stats-chart-total">
        {total === 1 ? "1 email in range" : `${total} emails in range`}
      </p>
    </div>
  );
}
