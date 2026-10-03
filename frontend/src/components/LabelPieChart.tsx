import { useState, type CSSProperties } from "react";
import { CLASSIFY_LABELS, CLASSIFY_LABEL_TITLES, LABEL_BAR_COLORS } from "../labels";

const RADIUS = 40;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

export default function LabelPieChart({
  counts,
  labels = CLASSIFY_LABELS,
}: {
  counts: Record<string, number>;
  labels?: readonly string[];
}) {
  const [hoveredKey, setHoveredKey] = useState<string | null>(null);
  const [showEmpty, setShowEmpty] = useState(false);
  const slices = labels.map((key) => ({
    key,
    value: counts[key] ?? 0,
    color: LABEL_BAR_COLORS[key as keyof typeof LABEL_BAR_COLORS],
    title: CLASSIFY_LABEL_TITLES[key as keyof typeof CLASSIFY_LABEL_TITLES],
  }));
  const total = slices.reduce((sum, slice) => sum + slice.value, 0);
  const ranked = slices.filter((slice) => slice.value > 0).sort((a, b) => b.value - a.value);
  const empty = slices.filter((slice) => slice.value === 0);
  const leading = ranked[0];
  const hovered = ranked.find((slice) => slice.key === hoveredKey) ?? null;
  const percent = (value: number) => (total ? Math.round((value * 100) / total) : 0);

  const gap = ranked.length > 1 ? 1.6 : 0;
  let offset = 0;
  const segments = slices
    .filter((slice) => slice.value > 0)
    .map((slice) => {
      const length = (slice.value / total) * CIRCUMFERENCE;
      const segment = { slice, dash: Math.max(length - gap, 0.4), offset };
      offset += length;
      return segment;
    });
  const focus = hovered ?? null;

  return (
    <div className="cat-dist stats-card">
      <header className="cat-dist-head">
        <div>
          <span className="stats-eyebrow">Overview</span>
          <h4>Category distribution</h4>
          <p>How your emails are split across AI categories in this range.</p>
        </div>
        <div className="cat-dist-badges">
          <span className="cat-dist-badge">
            <strong>{total.toLocaleString()}</strong> emails
          </span>
          <span className="cat-dist-badge is-accent">
            <strong>{ranked.length}</strong> of {slices.length} categories
          </span>
        </div>
      </header>

      <div className="cat-dist-body">
        <div className="cat-dist-chart">
          <div className="cat-dist-donut" role="img" aria-label={`${total} emails. ${ranked.map((slice) => `${slice.title}: ${slice.value}`).join(". ")}`}>
            <svg viewBox="0 0 100 100" aria-hidden="true">
              <circle className="cat-dist-track" cx="50" cy="50" r={RADIUS} />
              {segments.map(({ slice, dash, offset: start }) => (
                <circle
                  key={slice.key}
                  className={`cat-dist-segment${focus && focus.key !== slice.key ? " is-dimmed" : ""}${focus?.key === slice.key ? " is-active" : ""}`}
                  cx="50"
                  cy="50"
                  r={RADIUS}
                  stroke={slice.color}
                  strokeDasharray={`${dash} ${CIRCUMFERENCE}`}
                  strokeDashoffset={-start}
                  onMouseEnter={() => setHoveredKey(slice.key)}
                  onMouseLeave={() => setHoveredKey(null)}
                />
              ))}
            </svg>
            <div className="cat-dist-center">
              {focus ? (
                <>
                  <strong>{focus.value.toLocaleString()}</strong>
                  <span className="cat-dist-center-pct" style={{ color: focus.color }}>{percent(focus.value)}%</span>
                  <small>{focus.title}</small>
                </>
              ) : (
                <>
                  <strong>{total.toLocaleString()}</strong>
                  <small>{total === 1 ? "email" : "emails"}</small>
                </>
              )}
            </div>
          </div>
        </div>

        <div className="cat-dist-details">
          {leading ? (
            <div className="cat-dist-highlight" style={{ "--slice-color": leading.color } as CSSProperties}>
              <span className="cat-dist-highlight-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24"><path d="M4 18 9.5 12.5l3.5 3.5L20 9" /><path d="M15 9h5v5" /></svg>
              </span>
              <span className="cat-dist-highlight-copy">
                <small>Largest category</small>
                <strong>{leading.title}</strong>
              </span>
              <span className="cat-dist-highlight-stat">
                <strong>{percent(leading.value)}%</strong>
                <small>{leading.value.toLocaleString()} emails</small>
              </span>
            </div>
          ) : (
            <div className="cat-dist-highlight is-empty">
              <span className="cat-dist-highlight-copy">
                <small>No email data</small>
                <strong>Try a different date range</strong>
              </span>
            </div>
          )}

          <ul className="cat-dist-list">
            {ranked.map((slice, index) => (
              <li
                key={slice.key}
                className={hoveredKey === slice.key ? "is-active" : undefined}
                style={{ "--slice-color": slice.color } as CSSProperties}
                onMouseEnter={() => setHoveredKey(slice.key)}
                onMouseLeave={() => setHoveredKey(null)}
              >
                <span className="cat-dist-rank">{index + 1}</span>
                <span className="cat-dist-name">
                  <span className="cat-dist-swatch" aria-hidden="true" />
                  <span>{slice.title}</span>
                </span>
                <span className="cat-dist-bar" aria-hidden="true">
                  <span style={{ width: `${Math.max(percent(slice.value), 2)}%` }} />
                </span>
                <strong>{slice.value.toLocaleString()}</strong>
                <small>{percent(slice.value)}%</small>
              </li>
            ))}
          </ul>

          {empty.length > 0 && (
            <div className="cat-dist-empty">
              <button type="button" aria-expanded={showEmpty} onClick={() => setShowEmpty((value) => !value)}>
                <span className={`cat-dist-chevron${showEmpty ? " is-open" : ""}`} aria-hidden="true">›</span>
                {empty.length} {empty.length === 1 ? "category" : "categories"} with no emails
              </button>
              {showEmpty && (
                <div className="cat-dist-empty-chips">
                  {empty.map((slice) => (
                    <span key={slice.key} style={{ "--slice-color": slice.color } as CSSProperties}>
                      <span className="cat-dist-swatch" aria-hidden="true" />
                      {slice.title}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
