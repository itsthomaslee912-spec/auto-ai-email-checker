import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

type Parts = { y: number; m: number; d: number; hh: number; mm: number };

type ModernDateTimePickerProps = {
  label: string;
  value: string;
  onChange: (value: string) => void;
  min?: string;
  max?: string;
  minuteStep?: number;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
};

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

function partsFromDate(date: Date): Parts {
  return { y: date.getFullYear(), m: date.getMonth() + 1, d: date.getDate(), hh: date.getHours(), mm: date.getMinutes() };
}

function parseParts(value: string): Parts | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(value);
  if (!match) return null;
  const parts = { y: Number(match[1]), m: Number(match[2]), d: Number(match[3]), hh: Number(match[4]), mm: Number(match[5]) };
  const date = new Date(parts.y, parts.m - 1, parts.d, parts.hh, parts.mm);
  if (date.getFullYear() !== parts.y || date.getMonth() !== parts.m - 1 || date.getDate() !== parts.d || date.getHours() !== parts.hh || date.getMinutes() !== parts.mm) return null;
  return parts;
}

function toValue(parts: Parts): string {
  return `${parts.y}-${pad2(parts.m)}-${pad2(parts.d)}T${pad2(parts.hh)}:${pad2(parts.mm)}`;
}

function clampValue(value: string, min?: string, max?: string): string {
  if (min && value < min) return min.slice(0, 16);
  if (max && value > max) return max.slice(0, 16);
  return value;
}

function formatDisplay(value: string): string {
  const parts = parseParts(value);
  if (!parts) return "";
  return new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }).format(new Date(parts.y, parts.m - 1, parts.d, parts.hh, parts.mm));
}

function sameDate(a: Parts, date: Date): boolean {
  return a.y === date.getFullYear() && a.m === date.getMonth() + 1 && a.d === date.getDate();
}

export default function ModernDateTimePicker({
  label,
  value,
  onChange,
  min,
  max,
  minuteStep = 5,
  placeholder = "Choose date and time",
  disabled = false,
  className = "",
}: ModernDateTimePickerProps) {
  const labelId = useId();
  const dialogId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const fallback = useMemo(() => partsFromDate(new Date()), [open]);
  const selected = parseParts(value) ?? fallback;
  const [view, setView] = useState(() => ({ y: selected.y, m: selected.m }));
  const [anchor, setAnchor] = useState<{ top?: number; bottom?: number; left: number; width: number; maxHeight: number } | null>(null);

  useEffect(() => {
    if (!open) return;
    const current = parseParts(value) ?? partsFromDate(new Date());
    setView({ y: current.y, m: current.m });
    const rect = buttonRef.current?.getBoundingClientRect();
    if (rect) {
      const width = Math.min(320, window.innerWidth - 16);
      const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
      const spaceBelow = window.innerHeight - rect.bottom - 12;
      const spaceAbove = rect.top - 12;
      if (spaceBelow < 440 && spaceAbove > spaceBelow) setAnchor({ bottom: window.innerHeight - rect.top + 7, left, width, maxHeight: Math.max(220, spaceAbove) });
      else setAnchor({ top: rect.bottom + 7, left, width, maxHeight: Math.max(220, spaceBelow) });
    }

    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !popoverRef.current?.contains(target)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      buttonRef.current?.focus();
    };
    const close = () => setOpen(false);
    const closeOnScroll = (event: Event) => {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !popoverRef.current?.contains(target)) close();
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", closeOnScroll, true);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", closeOnScroll, true);
    };
  }, [open, value]);

  const monthStart = new Date(view.y, view.m - 1, 1);
  const gridStart = new Date(view.y, view.m - 1, 1 - monthStart.getDay());
  const days = Array.from({ length: 42 }, (_, index) => {
    const date = new Date(gridStart);
    date.setDate(gridStart.getDate() + index);
    return date;
  });
  const today = new Date();
  const monthLabel = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" }).format(monthStart);
  const step = Math.max(1, Math.min(60, minuteStep));
  const minutes = Array.from(new Set([...Array.from({ length: Math.ceil(60 / step) }, (_, index) => Math.min(59, index * step)), selected.mm])).sort((a, b) => a - b);
  const period = selected.hh >= 12 ? "PM" : "AM";
  const displayHour = selected.hh % 12 || 12;

  function update(parts: Parts) {
    onChange(clampValue(toValue(parts), min, max));
  }

  function shiftMonth(delta: number) {
    const next = new Date(view.y, view.m - 1 + delta, 1);
    setView({ y: next.getFullYear(), m: next.getMonth() + 1 });
  }

  function dayDisabled(date: Date) {
    const prefix = `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
    return Boolean((min && `${prefix}T23:59` < min) || (max && `${prefix}T00:00` > max));
  }

  function pickDay(date: Date) {
    update({ ...selected, y: date.getFullYear(), m: date.getMonth() + 1, d: date.getDate() });
    if (date.getMonth() + 1 !== view.m || date.getFullYear() !== view.y) setView({ y: date.getFullYear(), m: date.getMonth() + 1 });
  }

  function pickHour(raw: string) {
    const hour = Number(raw) % 12;
    update({ ...selected, hh: hour + (period === "PM" ? 12 : 0) });
  }

  function pickPeriod(next: "AM" | "PM") {
    if (next !== period) update({ ...selected, hh: (selected.hh + 12) % 24 });
  }

  function useNow() {
    const now = partsFromDate(new Date());
    update(now);
    setView({ y: now.y, m: now.m });
  }

  return (
    <div className={`modern-field${className ? ` ${className}` : ""}`} ref={rootRef}>
      <span className="modern-field-label" id={labelId}>{label}</span>
      <button
        ref={buttonRef}
        type="button"
        className={open ? "modern-control open" : "modern-control"}
        aria-labelledby={labelId}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? dialogId : undefined}
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
      >
        <span className={value ? "modern-control-value" : "modern-control-value placeholder"}>{value ? formatDisplay(value) : placeholder}</span>
        <svg viewBox="0 0 24 24" aria-hidden="true" className="modern-icon">
          <rect x="3.5" y="5" width="17" height="15" rx="2.5" />
          <path d="M8 3.5v3M16 3.5v3M3.5 10h17" />
          <path d="M8 14h3v3H8z" className="modern-icon-accent" />
        </svg>
      </button>
      {open && createPortal(
        <div
          ref={popoverRef}
          id={dialogId}
          className="modern-popover picker-pop"
          role="dialog"
          aria-modal="false"
          aria-label={`${label} picker`}
          style={anchor ? { position: "fixed", left: anchor.left, top: anchor.top ?? "auto", bottom: anchor.bottom ?? "auto", width: anchor.width, maxWidth: anchor.width, maxHeight: anchor.maxHeight } : undefined}
        >
          <div className="picker-month">
            <button type="button" className="picker-nav" onClick={() => shiftMonth(-1)} aria-label="Previous month"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m12.5 4.5-5 5 5 5" /></svg></button>
            <strong aria-live="polite">{monthLabel}</strong>
            <button type="button" className="picker-nav" onClick={() => shiftMonth(1)} aria-label="Next month"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m7.5 4.5 5 5-5 5" /></svg></button>
          </div>
          <div className="picker-weekdays" aria-hidden="true">{WEEKDAYS.map((day) => <span key={day}>{day.slice(0, 2)}</span>)}</div>
          <div className="picker-days" role="grid" aria-label={monthLabel}>
            {days.map((date) => {
              const dateParts = partsFromDate(date);
              const isSelected = Boolean(value) && sameDate(selected, date);
              const isToday = sameDate(partsFromDate(today), date);
              const outside = date.getMonth() + 1 !== view.m;
              const dateLabel = new Intl.DateTimeFormat(undefined, { dateStyle: "full" }).format(date);
              return (
                <button
                  key={toValue({ ...dateParts, hh: 0, mm: 0 })}
                  type="button"
                  role="gridcell"
                  className={`picker-day${isSelected ? " selected" : ""}${isToday ? " today" : ""}${outside ? " outside" : ""}`}
                  aria-label={dateLabel}
                  aria-selected={isSelected}
                  disabled={dayDisabled(date)}
                  onClick={() => pickDay(date)}
                >{date.getDate()}</button>
              );
            })}
          </div>
          <div className="picker-time-heading">
            <span><svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7" /><path d="M10 6v4l2.8 1.8" /></svg>Time</span>
            <button type="button" onClick={useNow}>Use current time</button>
          </div>
          <div className="picker-time">
            <label><span>Hour</span><select value={displayHour} onChange={(event) => pickHour(event.target.value)}>{Array.from({ length: 12 }, (_, index) => index + 1).map((hour) => <option key={hour} value={hour}>{pad2(hour)}</option>)}</select></label>
            <span className="picker-time-separator" aria-hidden="true">:</span>
            <label><span>Minute</span><select value={selected.mm} onChange={(event) => update({ ...selected, mm: Number(event.target.value) })}>{minutes.map((minute) => <option key={minute} value={minute}>{pad2(minute)}</option>)}</select></label>
            <div className="picker-period" aria-label="AM or PM">{(["AM", "PM"] as const).map((item) => <button key={item} type="button" className={period === item ? "selected" : ""} aria-pressed={period === item} onClick={() => pickPeriod(item)}>{item}</button>)}</div>
          </div>
          <div className="picker-footer">
            <button type="button" className="picker-today" onClick={() => pickDay(today)}>Today</button>
            <button type="button" className="picker-done" onClick={() => { setOpen(false); buttonRef.current?.focus(); }}>Done</button>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}
