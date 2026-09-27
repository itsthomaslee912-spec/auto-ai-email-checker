import type { EmailLabel, LabelTimeline } from "../api";
import { CLASSIFY_LABELS, CLASSIFY_LABEL_TITLES, LABEL_BAR_COLORS } from "../labels";

function bucketLabel(value: string, unit: LabelTimeline["bucket"], withYear: boolean): string {
  const hasZone = /Z$/i.test(value) || /[+-]\d{2}:\d{2}$/.test(value);
  const date = new Date(hasZone ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) return value;
  if (unit === "hour") {
    return new Intl.DateTimeFormat(undefined, { hour: "numeric" }).format(date);
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: withYear ? "numeric" : undefined,
  }).format(date);
}

export default function CategoryLineChart({ series }: { series: LabelTimeline[] }) {
  const byLabel = new Map(series.map((item) => [item.label, item]));
  const anchor = CLASSIFY_LABELS.map((key) => byLabel.get(key)).find((item) => item && item.buckets.length > 0);
  const buckets = anchor?.buckets ?? [];
  if (!anchor || buckets.length === 0) return null;

  const lines = CLASSIFY_LABELS.map((key) => {
    const timeline = byLabel.get(key);
    const lookup = new Map((timeline?.buckets ?? []).map((bucket) => [bucket.bucket, bucket.count]));
    return {
      key,
      counts: buckets.map((bucket) => lookup.get(bucket.bucket) ?? 0),
    };
  });
  const max = Math.max(1, ...lines.flatMap((line) => line.counts));
  const width = 640;
  const height = 220;
  const pad = { top: 16, right: 12, bottom: 32, left: 36 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const xAt = (index: number) =>
    pad.left + (buckets.length === 1 ? innerW / 2 : (index / (buckets.length - 1)) * innerW);
  const yAt = (count: number) => pad.top + innerH - (count / max) * innerH;
  const unit = anchor.bucket;
  const spanYears =
    unit === "day" &&
    buckets.length > 1 &&
    buckets[0].bucket.slice(0, 4) !== buckets[buckets.length - 1].bucket.slice(0, 4);
  const labelStep = Math.max(1, Math.ceil(buckets.length / 6));

  return (
    <div className="stats-timeline stats-card">
      <h4>All categories by {unit === "hour" ? "hour" : "date"}</h4>
      <div className="line-chart-scroll">
        <svg
          className="category-line"
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label={`All category counts by ${unit === "hour" ? "hour" : "date"}`}
          style={{ minWidth: Math.max(280, buckets.length * 28) }}
        >
          {[0, 0.5, 1].map((tick) => {
            const value = Math.round(max * tick);
            const y = yAt(max * tick);
            return (
              <g key={tick}>
                <line x1={pad.left} x2={width - pad.right} y1={y} y2={y} className="line-grid" />
                <text x={pad.left - 8} y={y + 4} className="line-axis" textAnchor="end">
                  {value}
                </text>
              </g>
            );
          })}
          {lines.map((line) => (
            <polyline
              key={line.key}
              points={line.counts.map((count, index) => `${xAt(index)},${yAt(count)}`).join(" ")}
              fill="none"
              stroke={LABEL_BAR_COLORS[line.key as EmailLabel]}
              strokeWidth={2.25}
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          ))}
          {buckets.map((bucket, index) =>
            index % labelStep === 0 || index === buckets.length - 1 ? (
              <text key={bucket.bucket} x={xAt(index)} y={height - 8} className="line-axis" textAnchor="middle">
                {bucketLabel(bucket.bucket, unit, spanYears)}
              </text>
            ) : null
          )}
        </svg>
      </div>
      <ul className="line-legend">
        {CLASSIFY_LABELS.map((key) => (
          <li key={key}>
            <span className="stats-pie-swatch" style={{ background: LABEL_BAR_COLORS[key] }} />
            {CLASSIFY_LABEL_TITLES[key]}
          </li>
        ))}
      </ul>
    </div>
  );
}
