import { useEffect, useId, useRef, useState } from "react";

type Parts = { y: number; m: number; d: number; hh: number; mm: number };

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

function parseParts(value: string): Parts {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value);
  if (!match) {
    const now = new Date();
    return {
      y: now.getFullYear(),
      m: now.getMonth() + 1,
      d: now.getDate(),
      hh: now.getHours(),
      mm: now.getMinutes(),
    };
  }
  return {
    y: Number(match[1]),
    m: Number(match[2]),
    d: Number(match[3]),
    hh: Number(match[4]),
    mm: Number(match[5]),
  };
}

function toValue(parts: Parts): string {
  return `${parts.y}-${pad2(parts.m)}-${pad2(parts.d)}T${pad2(parts.hh)}:${pad2(parts.mm)}`;
}

function formatDisplay(value: string): string {
  const parts = parseParts(value);
  const date = new Date(parts.y, parts.m - 1, parts.d, parts.hh, parts.mm);
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

const WEEKDAYS = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

export default function DateTimeField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const fieldId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<{ top?: number; bottom?: number; left: number; maxHeight: number } | null>(
    null
  );
  const [view, setView] = useState(() => {
    const parts = parseParts(value);
    return { y: parts.y, m: parts.m };
  });

  useEffect(() => {
    if (!open) return;
    const parts = parseParts(value);
    setView({ y: parts.y, m: parts.m });
    const rect = buttonRef.current?.getBoundingClientRect();
    if (rect) {
      const width = 264;
      const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
      const spaceBelow = window.innerHeight - rect.bottom - 12;
      const spaceAbove = rect.top - 12;
      if (spaceBelow < 340 && spaceAbove > spaceBelow) {
        setAnchor({ bottom: window.innerHeight - rect.top + 6, left, maxHeight: spaceAbove });
      } else {
        setAnchor({ top: rect.bottom + 6, left, maxHeight: Math.max(180, spaceBelow) });
      }
    }
    const onPointer = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const close = () => setOpen(false);
    const closeOnScroll = (event: Event) => {
      if (rootRef.current?.contains(event.target as Node)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", closeOnScroll, true);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", closeOnScroll, true);
    };
  }, [open, value]);

  const selected = parseParts(value);
  const daysInMonth = new Date(view.y, view.m, 0).getDate();
  const firstWeekday = new Date(view.y, view.m - 1, 1).getDay();
  const monthLabel = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" }).format(
    new Date(view.y, view.m - 1, 1)
  );

  function shiftMonth(delta: number) {
    const next = new Date(view.y, view.m - 1 + delta, 1);
    setView({ y: next.getFullYear(), m: next.getMonth() + 1 });
  }

  function pickDay(day: number) {
    onChange(toValue({ ...selected, y: view.y, m: view.m, d: day }));
  }

  function pickTime(part: "hh" | "mm", raw: string) {
    const max = part === "hh" ? 23 : 59;
    const next = Math.min(max, Math.max(0, Number(raw) || 0));
    onChange(toValue({ ...selected, [part]: next }));
  }

  return (
    <div className="modern-field" ref={rootRef}>
      <span className="modern-field-label" id={fieldId}>
        {label}
      </span>
      <button
        ref={buttonRef}
        type="button"
        className={open ? "modern-control open" : "modern-control"}
        aria-labelledby={fieldId}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span>{formatDisplay(value)}</span>
        <svg viewBox="0 0 24 24" aria-hidden="true" className="modern-icon">
          <rect x="3.5" y="5" width="17" height="15" rx="2" fill="none" stroke="currentColor" strokeWidth="1.6" />
          <path d="M8 3.5v3M16 3.5v3M3.5 10h17" fill="none" stroke="currentColor" strokeWidth="1.6" />
        </svg>
      </button>
      {open && (
        <div
          className="modern-popover picker-pop"
          role="dialog"
          aria-label={label}
          style={
            anchor
              ? {
                  position: "fixed",
                  left: anchor.left,
                  top: anchor.top,
                  bottom: anchor.bottom,
                  width: 264,
                  minWidth: 264,
                  maxWidth: 264,
                  boxSizing: "border-box",
                  maxHeight: anchor.maxHeight,
                  overflow: "auto",
                }
              : undefined
          }
        >
          <div className="picker-month">
            <button type="button" className="picker-nav" onClick={() => shiftMonth(-1)} aria-label="Previous month">
              ‹
            </button>
            <span>{monthLabel}</span>
            <button type="button" className="picker-nav" onClick={() => shiftMonth(1)} aria-label="Next month">
              ›
            </button>
          </div>
          <div className="picker-weekdays">
            {WEEKDAYS.map((day) => (
              <span key={day}>{day}</span>
            ))}
          </div>
          <div className="picker-days">
            {Array.from({ length: firstWeekday }, (_, index) => (
              <span key={`pad-${index}`} />
            ))}
            {Array.from({ length: daysInMonth }, (_, index) => {
              const day = index + 1;
              const isSelected = selected.y === view.y && selected.m === view.m && selected.d === day;
              return (
                <button
                  key={day}
                  type="button"
                  className={isSelected ? "picker-day selected" : "picker-day"}
                  onClick={() => pickDay(day)}
                >
                  {day}
                </button>
              );
            })}
          </div>
          <div className="picker-time">
            <label>
              Hour
              <input
                type="number"
                min={0}
                max={23}
                value={pad2(selected.hh)}
                onChange={(event) => pickTime("hh", event.target.value)}
              />
            </label>
            <label>
              Minute
              <input
                type="number"
                min={0}
                max={59}
                value={pad2(selected.mm)}
                onChange={(event) => pickTime("mm", event.target.value)}
              />
            </label>
          </div>
        </div>
      )}
    </div>
  );
}
