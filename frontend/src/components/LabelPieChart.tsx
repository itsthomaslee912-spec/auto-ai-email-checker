import { CLASSIFY_LABELS, CLASSIFY_LABEL_TITLES, LABEL_BAR_COLORS } from "../labels";

export default function LabelPieChart({
  counts,
  labels = CLASSIFY_LABELS,
}: {
  counts: Record<string, number>;
  labels?: readonly string[];
}) {
  const slices = labels.map((key) => ({
    key,
    value: counts[key] ?? 0,
    color: LABEL_BAR_COLORS[key as keyof typeof LABEL_BAR_COLORS],
    title: CLASSIFY_LABEL_TITLES[key as keyof typeof CLASSIFY_LABEL_TITLES],
  }));
  const total = slices.reduce((sum, slice) => sum + slice.value, 0);
  const activeCount = slices.filter((slice) => slice.value > 0).length;
  const leading = [...slices].sort((a, b) => b.value - a.value)[0];
  let cursor = 0;
  const gradient =
    total === 0
      ? "var(--bg-hover)"
      : `conic-gradient(${slices
          .map((slice) => {
            const start = (cursor / total) * 100;
            cursor += slice.value;
            const end = (cursor / total) * 100;
            return `${slice.color} ${start}% ${end}%`;
          })
          .join(", ")})`;

  return (
    <div className="stats-pie-block stats-card">
      <div className="stats-card-head">
        <div>
          <span className="stats-eyebrow">Overview</span>
          <h4>Category distribution</h4>
        </div>
        <span className="stats-total-badge">{activeCount} active</span>
      </div>

      <div className="stats-pie-row">
        <div
          className="stats-donut"
          style={{ background: gradient }}
          role="img"
          aria-label="All category counts"
        >
          <div className="stats-donut-hole">
            <strong>{total.toLocaleString()}</strong>
            <span>emails</span>
          </div>
        </div>

        <div className="stats-pie-details">
          <div className="stats-highlight">
            <span>Largest category</span>
            <strong>{total > 0 ? leading.title : "No email data"}</strong>
            <small>
              {total > 0
                ? `${leading.value.toLocaleString()} emails · ${Math.round((leading.value * 100) / total)}%`
                : "Try a different date range"}
            </small>
          </div>

          <ul className="stats-pie-legend">
            {slices.map((slice) => (
              <li key={slice.key} className={slice.value === 0 ? "is-empty" : undefined}>
                <span className="stats-pie-pick">
                  <span className="stats-pie-swatch" style={{ background: slice.color }} />
                  <span>{slice.title}</span>
                  <strong>{slice.value.toLocaleString()}</strong>
                  <small>{total ? `${Math.round((slice.value * 100) / total)}%` : "0%"}</small>
                </span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}
