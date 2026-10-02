import { FormEvent, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import EmailBody from "../components/EmailBody";
import CompactSelect from "../components/CompactSelect";
import ModernDateTimePicker from "../components/ModernDateTimePicker";
import {
  createCalendarEvent,
  eventsUrl,
  fetchCalendarEvents,
  fetchCalendarEventEmails,
  updateCalendarEvent,
  type CalendarEvent,
  type CalendarEventPayload,
  type EmailDetail,
  type Mailbox,
} from "../api";

type ViewMode = "month" | "week" | "day";

const VIEW_OPTIONS: { value: ViewMode; label: string }[] = [
  { value: "month", label: "Month" },
  { value: "week", label: "Week" },
  { value: "day", label: "Day" },
];

const COLORS = ["#2563eb", "#7c3aed", "#db2777", "#0891b2", "#059669", "#d97706", "#dc2626", "#4f46e5"];
const HOURS = Array.from({ length: 24 }, (_, hour) => hour);
const HOUR_HEIGHT = 64;
const DAY_MINUTES = 24 * 60;
const SNAP_MINUTES = 15;
const DEFAULT_EVENT_DURATION_MINUTES = 30;
const VISIBLE_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const CALENDAR_TIME_ZONE_KEY = "calendar-time-zone";
const SYSTEM_TIME_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const calendarMotionDelay = (duration: number) => typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? 0 : duration;
const TIME_ZONES = (() => {
  const intl = Intl as typeof Intl & { supportedValuesOf?: (key: "timeZone") => string[] };
  const supported = intl.supportedValuesOf?.("timeZone") ?? [
    "America/Los_Angeles", "America/Denver", "America/Chicago", "America/New_York",
    "Europe/London", "Europe/Paris", "Asia/Kolkata", "Asia/Shanghai", "Asia/Tokyo", "Australia/Sydney",
  ];
  return Array.from(new Set([SYSTEM_TIME_ZONE, "UTC", ...supported])).sort((a, b) => a.localeCompare(b));
})();

type ZonedParts = { year: number; month: number; day: number; hour: number; minute: number; second: number };
const zonedFormatters = new Map<string, Intl.DateTimeFormat>();

function validTimeZone(value: string | null): value is string {
  if (!value) return false;
  try { new Intl.DateTimeFormat(undefined, { timeZone: value }).format(); return true; }
  catch { return false; }
}
function loadCalendarTimeZone() {
  try { const saved = localStorage.getItem(CALENDAR_TIME_ZONE_KEY); if (validTimeZone(saved)) return saved; }
  catch { /* ignore unavailable storage */ }
  return SYSTEM_TIME_ZONE;
}
function partsInTimeZone(value: Date, timeZone: string): ZonedParts {
  let formatter = zonedFormatters.get(timeZone);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat("en-US-u-ca-gregory-nu-latn", {
      timeZone, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    });
    zonedFormatters.set(timeZone, formatter);
  }
  const values: Record<string, number> = {};
  for (const part of formatter.formatToParts(value)) {
    if (part.type !== "literal") values[part.type] = Number(part.value);
  }
  return { year: values.year, month: values.month, day: values.day, hour: values.hour, minute: values.minute, second: values.second } as ZonedParts;
}
function wallDateForInstant(value: Date, timeZone: string) {
  const part = partsInTimeZone(value, timeZone);
  return new Date(Date.UTC(part.year, part.month - 1, part.day, part.hour, part.minute, part.second));
}
function timeZoneOffsetAt(value: Date, timeZone: string) {
  const part = partsInTimeZone(value, timeZone);
  const valueToSecond = Math.floor(value.getTime() / 1000) * 1000;
  return Date.UTC(part.year, part.month - 1, part.day, part.hour, part.minute, part.second) - valueToSecond;
}
function instantForWallDate(value: Date, timeZone: string) {
  const wallTime = Date.UTC(value.getUTCFullYear(), value.getUTCMonth(), value.getUTCDate(), value.getUTCHours(), value.getUTCMinutes(), value.getUTCSeconds());
  let instant = wallTime;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const adjusted = wallTime - timeZoneOffsetAt(new Date(instant), timeZone);
    if (adjusted === instant) break;
    instant = adjusted;
  }
  return new Date(instant);
}
function wallDateFromInput(value: string) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!match) return new Date(Number.NaN);
  return new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]), Number(match[4]), Number(match[5])));
}

function startOfDay(date: Date) { return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate())); }
function addDays(date: Date, amount: number) { const next = new Date(date); next.setUTCDate(next.getUTCDate() + amount); return next; }
function startOfWeek(date: Date) { const out = startOfDay(date); out.setUTCDate(out.getUTCDate() - ((out.getUTCDay() + 6) % 7)); return out; }
function endExclusive(start: Date, view: ViewMode) {
  if (view === "day") return addDays(startOfDay(start), 1);
  if (view === "week") return addDays(startOfWeek(start), 7);
  const followingMonth = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + 1, 1));
  return addDays(followingMonth, 7 - followingMonth.getUTCDay());
}
function rangeStart(anchor: Date, view: ViewMode) {
  if (view === "day") return startOfDay(anchor);
  if (view === "week") return startOfWeek(anchor);
  const first = new Date(Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth(), 1));
  return startOfWeek(first);
}
function localInput(date: Date) {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}T${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
}
function formatWallTime(value: Date) { return new Intl.DateTimeFormat(undefined, { timeZone: "UTC", hour: "numeric", minute: "2-digit" }).format(value); }
function formatDateTime(value: string, timeZone: string) { return new Intl.DateTimeFormat(undefined, { timeZone, month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" }).format(new Date(value)); }
function formatInterviewTimeRange(event: CalendarEvent, timeZone: string) {
  const compactTime = (value: string) => {
    const instant = new Date(value);
    const minute = partsInTimeZone(instant, timeZone).minute;
    return new Intl.DateTimeFormat("en-US", {
      timeZone,
      hour: "numeric",
      ...(minute ? { minute: "2-digit" as const } : {}),
    }).format(instant).replace(/\s/g, "").toUpperCase();
  };
  return `${compactTime(event.start_at)} - ${compactTime(event.end_at)}`;
}
function accountColor(mailboxId: number) { return COLORS[Math.abs(mailboxId) % COLORS.length]; }
function overlapsDay(event: CalendarEvent, day: Date, timeZone: string) {
  const dayStart = instantForWallDate(startOfDay(day), timeZone);
  const dayEnd = instantForWallDate(addDays(startOfDay(day), 1), timeZone);
  return new Date(event.start_at) < dayEnd && new Date(event.end_at) > dayStart;
}
function sameDay(a: Date, b: Date) { return startOfDay(a).getTime() === startOfDay(b).getTime(); }
function isAllDay(event: CalendarEvent, timeZone: string) {
  const instantStart = new Date(event.start_at); const wallStart = wallDateForInstant(instantStart, timeZone); const end = new Date(event.end_at);
  return wallStart.getUTCHours() === 0 && wallStart.getUTCMinutes() === 0 && end.getTime() - instantStart.getTime() >= 23 * 60 * 60 * 1000;
}
function hourLabel(hour: number) {
  if (hour === 12) return "Midday";
  return new Intl.DateTimeFormat(undefined, { timeZone: "UTC", hour: "numeric" }).format(new Date(Date.UTC(2000, 0, 1, hour)));
}
function startsIn(value: string) {
  const minutes = Math.round((new Date(value).getTime() - Date.now()) / 60000);
  if (minutes <= 0) return "Happening now";
  if (minutes < 60) return `In ${minutes} minute${minutes === 1 ? "" : "s"}`;
  const hours = Math.floor(minutes / 60); const rest = minutes % 60;
  return `In ${hours} hour${hours === 1 ? "" : "s"}${rest ? ` ${rest} min` : ""}`;
}
function eventInitials(event: CalendarEvent) {
  return (event.company || event.title).split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]?.toUpperCase()).join("") || "EV";
}

function interviewKindLabel(event: CalendarEvent): string {
  return event.kind === "availability" ? "Available interview" : "Scheduled interview";
}

function applicationStatusLabel(event: CalendarEvent): string {
  if (event.application_status === "passed") return "Passed";
  if (event.application_status === "position_closed") return "Position closed";
  if (event.application_status === "rejected") return "Rejected";
  return "Active";
}

function ApplicationStatusIcon({ status }: { status: CalendarEvent["application_status"] }) {
  if (status === "passed") return <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 12h6M6 12.2a2 2 0 0 0 4 0M4.5 10.5h7l-1-1.5V6a2.5 2.5 0 0 0-5 0v3l-1 1.5Z" /></svg>;
  if (status === "position_closed") return <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 5.5h9v7h-9zM5.5 5.5V4a2.5 2.5 0 0 1 5 0v1.5M6 9h4" /></svg>;
  if (status === "rejected") return <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 4 8 8M12 4l-8 8" /></svg>;
  return <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="4.5" /><path d="m6 8 1.3 1.3L10.5 6" /></svg>;
}

function CanceledStatusIcon() {
  return <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.5" /><path d="m4.1 4.1 7.8 7.8" /></svg>;
}

function interviewType(event: CalendarEvent): { type: CalendarEvent["meeting_type"]; label: string } {
  const provider = event.meeting_provider?.trim().toLowerCase();
  const hasVideoPlatform = Boolean(event.meeting_url || (provider && !provider.includes("phone") && !provider.includes("in person")));
  if (event.meeting_type === "video" || hasVideoPlatform) return { type: "video", label: "Video call" };
  if (event.meeting_type === "phone" || event.phone_number) return { type: "phone", label: "Phone call" };
  if (event.meeting_type === "in_person") return { type: "in_person", label: "In-person interview" };
  return { type: "unspecified", label: "Not specified" };
}

function meetingProviderLabel(event: CalendarEvent): string {
  const provider = event.meeting_provider?.trim();
  if (provider) {
    const normalized = provider.toLowerCase();
    if (normalized.includes("google") || normalized === "meet") return "Google Meet";
    if (normalized.includes("zoom")) return "Zoom";
    if (normalized.includes("teams")) return "Microsoft Teams";
    if (normalized.includes("webex")) return "Cisco Webex";
    if (normalized.includes("whereby")) return "Whereby";
    if (normalized.includes("goto")) return "GoTo Meeting";
    return provider;
  }
  if (!event.meeting_url) return "—";
  try {
    const host = new URL(event.meeting_url).hostname.toLowerCase().replace(/^www\./, "");
    if (host === "meet.google.com") return "Google Meet";
    if (host === "zoom.us" || host.endsWith(".zoom.us") || host === "zoom.com" || host.endsWith(".zoom.com")) return "Zoom";
    if (host === "teams.microsoft.com" || host === "teams.live.com") return "Microsoft Teams";
    if (host === "webex.com" || host.endsWith(".webex.com")) return "Cisco Webex";
    if (host === "whereby.com" || host.endsWith(".whereby.com")) return "Whereby";
    if (host === "meet.goto.com" || host.endsWith(".gotomeeting.com")) return "GoTo Meeting";
    return host;
  } catch {
    return "—";
  }
}

function MeetingTypeIcon({ type }: { type: CalendarEvent["meeting_type"] }) {
  if (type === "phone") return <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5.1 2.3 3.4 3.5c-.7.5-.9 1.4-.5 2.2 1.6 3.3 4.1 5.8 7.4 7.4.8.4 1.7.2 2.2-.5l1.2-1.7-3-2-1.2 1.3c-1.6-.9-2.8-2.1-3.7-3.7l1.3-1.2-2-3Z" /></svg>;
  if (type === "video") return <svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2" y="4" width="8.5" height="8" rx="2" /><path d="m10.5 6.6 3.5-2v6.8l-3.5-2" /></svg>;
  if (type === "in_person") return <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 14s4-3.8 4-7.5a4 4 0 1 0-8 0C4 10.2 8 14 8 14Z" /><circle cx="8" cy="6.5" r="1.35" /></svg>;
  return <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.5" /><path d="M8 5.2v3.2l2.1 1.3" /></svg>;
}

function CalendarViewIcon({ view }: { view: ViewMode }) {
  if (view === "month") return <svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2" y="2.5" width="12" height="11" rx="2" /><path d="M2 6h12M6 6v7.5M10 6v7.5M2 9.75h12" /></svg>;
  if (view === "week") return <svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2" y="2.5" width="12" height="11" rx="2" /><path d="M2 6h12M6 6v7.5M10 6v7.5" /></svg>;
  return <svg viewBox="0 0 16 16" aria-hidden="true"><rect x="3.5" y="2.5" width="9" height="11" rx="2" /><path d="M3.5 6h9" /></svg>;
}

function CalendarViewPicker({ value, onChange }: { value: ViewMode; onChange: (view: ViewMode) => void }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const selectedIndex = VIEW_OPTIONS.findIndex((option) => option.value === value);
  const selected = VIEW_OPTIONS[selectedIndex] ?? VIEW_OPTIONS[1];

  useEffect(() => {
    if (!open) return;
    optionRefs.current[selectedIndex]?.focus();
    const onPointerDown = (event: globalThis.MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      buttonRef.current?.focus();
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, selectedIndex]);

  const choose = (next: ViewMode) => {
    onChange(next);
    setOpen(false);
    buttonRef.current?.focus();
  };
  const onTriggerKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    setOpen(true);
  };
  const onOptionKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index;
    if (event.key === "ArrowDown") next = (index + 1) % VIEW_OPTIONS.length;
    else if (event.key === "ArrowUp") next = (index - 1 + VIEW_OPTIONS.length) % VIEW_OPTIONS.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = VIEW_OPTIONS.length - 1;
    else if (event.key === "Tab") { setOpen(false); return; }
    else return;
    event.preventDefault();
    optionRefs.current[next]?.focus();
  };

  return <div className="calendar-view-picker" ref={rootRef}>
    <button ref={buttonRef} className="calendar-view-trigger" type="button" aria-label={`Calendar view: ${selected.label}`} aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((current) => !current)} onKeyDown={onTriggerKeyDown}>
      <CalendarViewIcon view={value} /><span>{selected.label}</span><svg className="calendar-view-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg>
    </button>
    {open && <div className="calendar-view-menu" role="menu" aria-label="Calendar view">
      {VIEW_OPTIONS.map((option, index) => <button ref={(node) => { optionRefs.current[index] = node; }} className={`calendar-view-option ${value === option.value ? "selected" : ""}`} type="button" role="menuitemradio" aria-checked={value === option.value} onClick={() => choose(option.value)} onKeyDown={(event) => onOptionKeyDown(event, index)} key={option.value}>
        <CalendarViewIcon view={option.value} /><span>{option.label}</span><span className="calendar-view-check" aria-hidden="true">✓</span>
      </button>)}
    </div>}
  </div>;
}

function timeZoneLabel(zone: string) {
  return `${zone.replaceAll("_", " ")}${zone === SYSTEM_TIME_ZONE ? " (Local)" : ""}`;
}

function CalendarTimeZonePicker({ value, onChange }: { value: string; onChange: (timeZone: string) => void }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const filteredZones = useMemo(() => {
    const term = query.trim().toLowerCase();
    return term ? TIME_ZONES.filter((zone) => timeZoneLabel(zone).toLowerCase().includes(term)) : TIME_ZONES;
  }, [query]);

  useEffect(() => {
    if (!open) return;
    searchRef.current?.focus();
    requestAnimationFrame(() => optionRefs.current[filteredZones.indexOf(value)]?.scrollIntoView({ block: "nearest" }));
    const onPointerDown = (event: globalThis.MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      setQuery("");
      buttonRef.current?.focus();
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, value]);

  const choose = (zone: string) => {
    onChange(zone);
    setOpen(false);
    setQuery("");
    buttonRef.current?.focus();
  };
  const focusOption = (index: number) => optionRefs.current[Math.max(0, Math.min(filteredZones.length - 1, index))]?.focus();
  const onSearchKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    const selectedIndex = filteredZones.indexOf(value);
    focusOption(selectedIndex >= 0 ? selectedIndex : event.key === "ArrowDown" ? 0 : filteredZones.length - 1);
  };
  const onOptionKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index;
    if (event.key === "ArrowDown") next = (index + 1) % filteredZones.length;
    else if (event.key === "ArrowUp") next = (index - 1 + filteredZones.length) % filteredZones.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = filteredZones.length - 1;
    else return;
    event.preventDefault();
    focusOption(next);
  };

  return <div className="calendar-timezone-picker" ref={rootRef}>
    <button ref={buttonRef} className="calendar-timezone-trigger" type="button" title={`Calendar timezone: ${value}`} aria-label={`Calendar timezone: ${timeZoneLabel(value)}`} aria-haspopup="listbox" aria-expanded={open} onClick={() => { setOpen((current) => !current); setQuery(""); }} onKeyDown={(event) => { if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); setOpen(true); } }}>
      <svg className="calendar-timezone-icon" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.75" /><path d="M2.5 8h11M8 2.25c1.55 1.55 2.35 3.46 2.35 5.75S9.55 12.2 8 13.75C6.45 12.2 5.65 10.29 5.65 8S6.45 3.8 8 2.25Z" /></svg>
      <span>{timeZoneLabel(value)}</span><svg className="calendar-timezone-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg>
    </button>
    {open && <div className="calendar-timezone-menu">
      <div className="calendar-timezone-search-wrap"><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4" /><path d="m10 10 3 3" /></svg><input ref={searchRef} value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={onSearchKeyDown} placeholder="Search timezones" aria-label="Search timezones" /></div>
      <div className="calendar-timezone-options" role="listbox" aria-label="Calendar timezone">
        {filteredZones.map((zone, index) => <button ref={(node) => { optionRefs.current[index] = node; }} className={`calendar-timezone-option ${value === zone ? "selected" : ""}`} type="button" role="option" aria-selected={value === zone} onClick={() => choose(zone)} onKeyDown={(event) => onOptionKeyDown(event, index)} key={zone}><span>{timeZoneLabel(zone)}</span><span className="calendar-timezone-check" aria-hidden="true">✓</span></button>)}
        {!filteredZones.length && <div className="calendar-timezone-empty">No matching timezone</div>}
      </div>
    </div>}
  </div>;
}

function layoutTimedEvents(events: CalendarEvent[], day: Date, hourHeight: number, timeZone: string) {
  const dayStart = instantForWallDate(startOfDay(day), timeZone).getTime();
  const dayEnd = instantForWallDate(addDays(startOfDay(day), 1), timeZone).getTime();
  const minutesInDay = (instant: number, end: boolean) => {
    if (instant <= dayStart) return 0;
    if (instant >= dayEnd) return DAY_MINUTES;
    const wall = wallDateForInstant(new Date(instant), timeZone);
    const minutes = wall.getUTCHours() * 60 + wall.getUTCMinutes() + wall.getUTCSeconds() / 60;
    return end && minutes === 0 ? DAY_MINUTES : minutes;
  };
  const items = events.filter((event) => !isAllDay(event, timeZone)).map((event) => {
    const start = Math.max(dayStart, new Date(event.start_at).getTime());
    const end = Math.min(dayEnd, new Date(event.end_at).getTime());
    return { event, start, end, startMinutes: minutesInDay(start, false), endMinutes: minutesInDay(end, true), column: 0, columns: 1 };
  }).sort((a, b) => a.start - b.start || b.end - a.end);
  let group: typeof items = []; let groupEnd = 0;
  const finishGroup = () => {
    if (!group.length) return;
    const columnEnds: number[] = [];
    for (const item of group) {
      let column = columnEnds.findIndex((end) => end <= item.start);
      if (column < 0) { column = columnEnds.length; columnEnds.push(item.end); } else columnEnds[column] = item.end;
      item.column = column;
    }
    for (const item of group) item.columns = columnEnds.length;
    group = [];
  };
  for (const item of items) {
    if (group.length && item.start >= groupEnd) finishGroup();
    group.push(item); groupEnd = Math.max(groupEnd, item.end);
  }
  finishGroup();
  return items.map((item) => ({
    event: item.event,
    position: {
      top: `${(item.startMinutes / 60) * hourHeight}px`,
      height: `${Math.max(28, ((item.endMinutes - item.startMinutes) / 60) * hourHeight)}px`,
      left: `calc(${(item.column / item.columns) * 100}% + 3px)`,
      width: `calc(${100 / item.columns}% - 6px)`,
      right: "auto",
    } as React.CSSProperties,
  }));
}

function EventCard({ event, timeZone, compact, timed, position, selected, activeHoverId, onHoverActivate, onHoverDeactivate, onSelect }: { event: CalendarEvent; timeZone: string; compact?: boolean; timed?: boolean; position?: React.CSSProperties; selected?: boolean; activeHoverId: string | null; onHoverActivate: (hoverId: string) => void; onHoverDeactivate: (hoverId: string) => void; onSelect: (event: CalendarEvent) => void }) {
  const eventRef = useRef<HTMLDivElement>(null);
  const hoverId = useId();
  const [hoverPosition, setHoverPosition] = useState<{ left: number; top: number; above: boolean } | null>(null);
  const [hoverVisible, setHoverVisible] = useState(false);
  const hoverHideTimer = useRef<number | null>(null);
  const hoverFrame = useRef<number | null>(null);
  const color = accountColor(event.mailbox_id);
  const canceled = event.interview_status === "cancellation";
  const outcome = event.application_status !== "active" ? event.application_status : null;
  const outcomeClass = outcome ? `status-${outcome.replaceAll("_", "-")}` : "";
  const outcomeLabel = applicationStatusLabel(event);
  const type = interviewType(event);
  const durationMinutes = Math.max(0, (new Date(event.end_at).getTime() - new Date(event.start_at).getTime()) / 60000);
  const getHoverPosition = useCallback(() => {
    const rect = eventRef.current?.getBoundingClientRect();
    if (!rect) return null;
    const cardWidth = Math.min(288, window.innerWidth - 24);
    const above = window.innerHeight - rect.bottom < 240 && rect.top > window.innerHeight - rect.bottom;
    return {
      left: Math.max(12, Math.min(window.innerWidth - cardWidth - 12, rect.left + (rect.width - cardWidth) / 2)),
      top: above ? rect.top - 8 : rect.bottom + 8,
      above,
    };
  }, []);
  const showHover = useCallback(() => {
    if (hoverHideTimer.current != null) window.clearTimeout(hoverHideTimer.current);
    if (hoverFrame.current != null) window.cancelAnimationFrame(hoverFrame.current);
    const nextPosition = getHoverPosition();
    if (!nextPosition) return;
    onHoverActivate(hoverId);
    setHoverPosition(nextPosition);
    setHoverVisible(false);
    hoverFrame.current = window.requestAnimationFrame(() => {
      setHoverVisible(true);
      hoverFrame.current = null;
    });
  }, [getHoverPosition, hoverId, onHoverActivate]);
  const hideHover = useCallback(() => {
    if (hoverFrame.current != null) window.cancelAnimationFrame(hoverFrame.current);
    if (hoverHideTimer.current != null) window.clearTimeout(hoverHideTimer.current);
    onHoverDeactivate(hoverId);
    setHoverVisible(false);
    hoverHideTimer.current = window.setTimeout(() => {
      setHoverPosition(null);
      hoverHideTimer.current = null;
    }, calendarMotionDelay(150));
  }, [hoverId, onHoverDeactivate]);
  useEffect(() => {
    if (activeHoverId == null || activeHoverId === hoverId) return;
    if (hoverFrame.current != null) window.cancelAnimationFrame(hoverFrame.current);
    if (hoverHideTimer.current != null) window.clearTimeout(hoverHideTimer.current);
    hoverFrame.current = null;
    hoverHideTimer.current = null;
    setHoverVisible(false);
    setHoverPosition(null);
  }, [activeHoverId, hoverId]);
  useEffect(() => {
    if (!hoverPosition) return;
    const reposition = () => {
      const nextPosition = getHoverPosition();
      if (nextPosition) setHoverPosition(nextPosition);
    };
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [hoverPosition, getHoverPosition]);
  useEffect(() => () => {
    if (hoverHideTimer.current != null) window.clearTimeout(hoverHideTimer.current);
    if (hoverFrame.current != null) window.cancelAnimationFrame(hoverFrame.current);
  }, []);
  const selectEvent = () => { onHoverDeactivate(hoverId); setHoverVisible(false); setHoverPosition(null); onSelect(event); };
  return <>
    <div
    ref={eventRef}
    className={`calendar-event ${event.kind} ${canceled ? "canceled" : ""} ${outcomeClass} ${compact ? "compact" : ""} ${timed ? "timed" : ""} ${selected ? "selected" : ""} has-meeting-type ${outcome ? "has-outcome" : ""} ${timed && durationMinutes <= 30 ? "very-short" : timed && durationMinutes < 60 ? "short" : ""}`}
    style={{ "--account-color": color, ...position } as React.CSSProperties}
    onPointerDown={(e) => e.stopPropagation()}
    onClick={(e) => { e.stopPropagation(); selectEvent(); }}
    onKeyDown={(e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); e.stopPropagation(); selectEvent(); } }}
    onMouseEnter={showHover}
    onMouseLeave={() => { if (document.activeElement !== eventRef.current) hideHover(); }}
    onFocus={showHover}
    onBlur={hideHover}
    role="button"
    tabIndex={0}
    aria-pressed={selected}
    aria-label={`${interviewKindLabel(event)}, ${event.company || "Company not specified"}, ${type.label}, ${canceled ? "Canceled, " : ""}${outcomeLabel}`}
    aria-describedby={hoverPosition ? hoverId : undefined}
  >
    <span className="calendar-event-company">{event.company || "Company not specified"}</span>
    {canceled && <span className="event-canceled-badge"><CanceledStatusIcon />Canceled</span>}
    <span className={`event-type-badge meeting-${type.type}`} role="img" aria-label={type.label}><MeetingTypeIcon type={type.type} /></span>
    {outcome && <span className="event-outcome-badge" role="img" aria-label={outcomeLabel} title={outcomeLabel}><ApplicationStatusIcon status={outcome} /></span>}
  </div>
    {hoverPosition && createPortal(
      <div
        id={hoverId}
        className={`calendar-event-hover-card ${hoverPosition.above ? "above" : "below"} ${hoverVisible ? "is-visible" : ""}`}
        role="tooltip"
        style={{ left: hoverPosition.left, top: hoverPosition.top }}
      >
        <dl>
          <div className="calendar-hover-time"><dt>Interview time</dt><dd>{formatInterviewTimeRange(event, timeZone)}</dd></div>
          <div><dt>Account</dt><dd>{event.email_account || "—"}</dd></div>
          <div><dt>Company</dt><dd>{event.company || "—"}</dd></div>
          <div><dt>Role</dt><dd>{event.role || "—"}</dd></div>
          <div className={`calendar-hover-type meeting-${type.type}`}>
            <dt>Interview type</dt>
            <dd><MeetingTypeIcon type={type.type} /><span>{type.label}</span></dd>
          </div>
          {canceled && <div className="calendar-hover-canceled"><dt>Interview status</dt><dd><CanceledStatusIcon /><span>Canceled</span></dd></div>}
          {outcome && <div className={`calendar-hover-status ${outcomeClass}`}><dt>Application status</dt><dd><ApplicationStatusIcon status={outcome} /><span>{outcomeLabel}</span></dd></div>}
        </dl>
      </div>,
      document.body,
    )}
  </>;
}

type AccountInterviewCount = {
  mailboxId: number;
  email: string;
  count: number;
  color: string;
};

function ScheduledInterviewChart({ accounts }: { accounts: AccountInterviewCount[] }) {
  const total = accounts.reduce((sum, account) => sum + account.count, 0);
  const activeAccounts = accounts.filter((account) => account.count > 0);
  let cursor = 0;
  const chartFill = total === 0
    ? "var(--calendar-border-soft)"
    : `conic-gradient(${activeAccounts.map((account) => {
      const start = (cursor / total) * 100;
      cursor += account.count;
      const end = (cursor / total) * 100;
      return `${account.color} ${start}% ${end}%`;
    }).join(", ")})`;
  const ariaSummary = total === 0
    ? "No scheduled interviews in this view"
    : `${total} scheduled interview${total === 1 ? "" : "s"} in this view. ${activeAccounts.map((account) => `${account.email}: ${account.count}`).join(". ")}`;

  return <section className="calendar-interview-summary" aria-label="Scheduled interviews by email account">
    <div className="interview-chart-card">
      <div className="interview-donut-wrap">
        <div className="interview-donut" style={{ background: chartFill }} role="img" aria-label={ariaSummary}>
          <div className="interview-donut-hole"><strong>{total}</strong><span>scheduled</span></div>
        </div>
      </div>
      <ul className="interview-account-list" aria-label="Scheduled interview counts by email account">
        {accounts.map((account) => {
          const percentage = total > 0 ? Math.round((account.count / total) * 100) : 0;
          return <li className={account.count === 0 ? "is-empty" : undefined} key={account.mailboxId} title={account.email}>
            <span className="interview-account-swatch" style={{ background: account.color }} aria-hidden="true" />
            <span className="interview-account-copy"><strong>{account.email}</strong><small>{percentage}% of scheduled</small></span>
            <span className="interview-account-count" aria-label={`${account.count} scheduled`}>{account.count}</span>
          </li>;
        })}
      </ul>
      {!accounts.length && <p className="interview-chart-empty">Connect an email account to see interview activity.</p>}
    </div>
  </section>;
}

type Editor = { id?: number; mailbox_id: number; title: string; company: string; role: string; interviewer: string; work_model: CalendarEvent["work_model"]; application_status: CalendarEvent["application_status"]; start: string; end: string; description: string; job_url: string; meeting_type: "video" | "phone"; meeting_provider: string; meeting_url: string; phone_number: string; phone_access_code: string };
function editorFor(date: Date, mailboxId: number, timeZone: string, event?: CalendarEvent, selectedEnd?: Date): Editor {
  const start = event ? wallDateForInstant(new Date(event.start_at), timeZone) : selectedEnd ? new Date(date) : new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate(), 9));
  const end = event ? wallDateForInstant(new Date(event.end_at), timeZone) : selectedEnd ?? new Date(start.getTime() + DEFAULT_EVENT_DURATION_MINUTES * 60_000);
  return { id: event?.id, mailbox_id: event?.mailbox_id ?? mailboxId, title: event?.title ?? "Interview", company: event?.company ?? "", role: event?.role ?? "", interviewer: event?.interviewer ?? "", work_model: event?.work_model ?? "unspecified", application_status: event?.application_status ?? "active", start: localInput(start), end: localInput(end), description: event?.description ?? "", job_url: event?.job_url ?? "", meeting_type: event?.meeting_type === "phone" ? "phone" : "video", meeting_provider: event?.meeting_provider ?? "", meeting_url: event?.meeting_url ?? "", phone_number: event?.phone_number ?? "", phone_access_code: event?.phone_access_code ?? "" };
}

type DragSelection = { dayIndex: number; startMinutes: number; endMinutes: number };

export default function CalendarPage({ mailboxes, refreshKey, onClose }: { mailboxes: Mailbox[]; refreshKey: number; onClose: () => void }) {
  const [view, setView] = useState<ViewMode>(() => (localStorage.getItem("calendar-view") as ViewMode) || "week");
  const [timeZone, setTimeZone] = useState(loadCalendarTimeZone);
  const [anchor, setAnchor] = useState(() => wallDateForInstant(new Date(), timeZone));
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [loading, setLoading] = useState(true); const [error, setError] = useState("");
  const [selected, setSelected] = useState<CalendarEvent | null>(null);
  const [activeHoverId, setActiveHoverId] = useState<string | null>(null);
  const [detailsClosing, setDetailsClosing] = useState(false);
  const [copiedMeetingEventId, setCopiedMeetingEventId] = useState<number | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarClosing, setSidebarClosing] = useState(false);
  const [emailMessages, setEmailMessages] = useState<EmailDetail[]>([]); const [emailLoading, setEmailLoading] = useState(false); const [emailError, setEmailError] = useState(""); const [expandedEmailId, setExpandedEmailId] = useState<number | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null); const [saving, setSaving] = useState(false); const [saveError, setSaveError] = useState("");
  const activateEventHover = useCallback((hoverId: string) => setActiveHoverId(hoverId), []);
  const deactivateEventHover = useCallback((hoverId: string) => setActiveHoverId((current) => current === hoverId ? null : current), []);
  const [dragSelection, setDragSelection] = useState<DragSelection | null>(null);
  const [hourHeight, setHourHeight] = useState(HOUR_HEIGHT);
  const refreshRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const detailsCloseTimer = useRef<number | null>(null);
  const sidebarCloseTimer = useRef<number | null>(null);
  const timelineRef = useRef<HTMLElement | null>(null);
  const timelineHeaderRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{ dayIndex: number; anchorMinutes: number } | null>(null);
  const start = useMemo(() => rangeStart(anchor, view), [anchor, view]);
  const end = useMemo(() => view === "month" ? addDays(start, 42) : endExclusive(start, view), [start, view]);

  const load = useCallback(async () => {
    abortRef.current?.abort();
    const request = ++refreshRef.current; const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true); setError("");
    try {
      const rows = await fetchCalendarEvents(instantForWallDate(start, timeZone).toISOString(), instantForWallDate(end, timeZone).toISOString(), controller.signal);
      if (request === refreshRef.current) {
        setEvents(rows);
        setSelected((current) => current ? rows.find((event) => event.id === current.id) ?? current : null);
      }
    }
    catch (err) { if (request === refreshRef.current && (err as Error).name !== "AbortError") setError(err instanceof Error ? err.message : "Failed to load calendar"); }
    finally { if (request === refreshRef.current) setLoading(false); }
  }, [start.getTime(), end.getTime(), timeZone]);
  useEffect(() => { void load(); return () => abortRef.current?.abort(); }, [load, refreshKey]);
  useEffect(() => { const source = new EventSource(eventsUrl()); source.addEventListener("calendar.changed", () => void load()); return () => source.close(); }, [load]);
  useEffect(() => { localStorage.setItem("calendar-view", view); }, [view]);
  useEffect(() => { try { localStorage.setItem(CALENDAR_TIME_ZONE_KEY, timeZone); } catch { /* ignore unavailable storage */ } }, [timeZone]);
  useEffect(() => () => {
    if (detailsCloseTimer.current != null) window.clearTimeout(detailsCloseTimer.current);
    if (sidebarCloseTimer.current != null) window.clearTimeout(sidebarCloseTimer.current);
  }, []);
  useLayoutEffect(() => {
    if (view === "month") return;
    const timeline = timelineRef.current;
    const header = timelineHeaderRef.current;
    if (!timeline || !header) return;
    const resizeTimeline = () => {
      const availableBodyHeight = Math.max(0, timeline.clientHeight - header.offsetHeight);
      const nextHourHeight = Math.max(HOUR_HEIGHT, availableBodyHeight / HOURS.length);
      const roundedHourHeight = Math.round(nextHourHeight * 100) / 100;
      setHourHeight((current) => Math.abs(current - roundedHourHeight) < .01 ? current : roundedHourHeight);
    };
    resizeTimeline();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(resizeTimeline);
    observer.observe(timeline);
    observer.observe(header);
    return () => observer.disconnect();
  }, [view]);
  useEffect(() => {
    if (view !== "month" && timelineRef.current) timelineRef.current.scrollTop = 7 * hourHeight;
    setDragSelection(null); dragRef.current = null;
  }, [view, start.getTime(), hourHeight]);
  useEffect(() => {
    setEmailMessages([]); setEmailError(""); setExpandedEmailId(null);
    if (!selected) return;
    let active = true; setEmailLoading(true);
    fetchCalendarEventEmails(selected.id).then((messages) => {
      if (!active) return;
      setEmailMessages([...messages].sort((a, b) => {
        const bTime = new Date(b.received_at || b.created_at || 0).getTime();
        const aTime = new Date(a.received_at || a.created_at || 0).getTime();
        return bTime - aTime || b.id - a.id;
      }));
    }).catch((err) => active && setEmailError(err instanceof Error ? err.message : "Failed to load associated emails")).finally(() => active && setEmailLoading(false));
    return () => { active = false; };
  }, [selected]);

  const days = useMemo(() => {
    if (view === "day") return [start];
    return Array.from({ length: view === "month" ? 42 : 7 }, (_, i) => addDays(start, i));
  }, [start, view]);
  const miniMonthStart = useMemo(() => startOfWeek(new Date(Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth(), 1))), [anchor.getUTCFullYear(), anchor.getUTCMonth()]);
  const miniMonthDays = useMemo(() => Array.from({ length: 42 }, (_, i) => addDays(miniMonthStart, i)), [miniMonthStart.getTime()]);
  const scheduledInterviewsByAccount = useMemo(() => mailboxes.filter((mailbox) => mailbox.is_active).map((mailbox) => ({
    mailboxId: mailbox.id,
    email: mailbox.email_address,
    color: accountColor(mailbox.id),
    count: events.filter((event) => event.mailbox_id === mailbox.id && event.kind === "confirmed" && event.interview_status !== "cancellation").length,
  })), [events, mailboxes]);
  const nextMeeting = useMemo(() => events.filter((event) => event.kind === "confirmed" && event.interview_status !== "cancellation" && new Date(event.end_at).getTime() > Date.now()).sort((a, b) => new Date(a.start_at).getTime() - new Date(b.start_at).getTime())[0] ?? null, [events]);
  const nowInTimeZone = wallDateForInstant(new Date(), timeZone);
  const move = (amount: number) => setAnchor((date) => { const out = new Date(date); if (view === "month") out.setUTCMonth(out.getUTCMonth() + amount); else out.setUTCDate(out.getUTCDate() + amount * (view === "week" ? 7 : 1)); return out; });
  const defaultMailboxId = mailboxes.find((m) => m.is_active)?.id;
  const openCreate = (day: Date) => { if (defaultMailboxId != null) setEditor(editorFor(day, defaultMailboxId, timeZone)); };
  const minutesAtPointer = (element: HTMLDivElement, clientY: number) => {
    const rect = element.getBoundingClientRect();
    const raw = ((clientY - rect.top) / rect.height) * DAY_MINUTES;
    return Math.max(0, Math.min(DAY_MINUTES - SNAP_MINUTES, Math.round(raw / SNAP_MINUTES) * SNAP_MINUTES));
  };
  const beginTimeDrag = (dayIndex: number, event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || defaultMailboxId == null) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    const anchorMinutes = minutesAtPointer(event.currentTarget, event.clientY);
    dragRef.current = { dayIndex, anchorMinutes };
    setDragSelection({ dayIndex, startMinutes: anchorMinutes, endMinutes: Math.min(DAY_MINUTES, anchorMinutes + DEFAULT_EVENT_DURATION_MINUTES) });
  };
  const updateTimeDrag = (dayIndex: number, event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.dayIndex !== dayIndex) return;
    const currentMinutes = minutesAtPointer(event.currentTarget, event.clientY);
    setDragSelection({ dayIndex, startMinutes: Math.min(drag.anchorMinutes, currentMinutes), endMinutes: drag.anchorMinutes === currentMinutes ? Math.min(DAY_MINUTES, currentMinutes + DEFAULT_EVENT_DURATION_MINUTES) : Math.max(drag.anchorMinutes, currentMinutes) });
  };
  const finishTimeDrag = (dayIndex: number, event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.dayIndex !== dayIndex || defaultMailboxId == null) return;
    const currentMinutes = minutesAtPointer(event.currentTarget, event.clientY);
    const startMinutes = Math.min(drag.anchorMinutes, currentMinutes);
    const endMinutes = drag.anchorMinutes === currentMinutes ? Math.min(DAY_MINUTES, startMinutes + DEFAULT_EVENT_DURATION_MINUTES) : Math.max(drag.anchorMinutes, currentMinutes);
    const rangeStart = new Date(days[dayIndex]); rangeStart.setUTCMinutes(startMinutes);
    const rangeEnd = new Date(days[dayIndex]); rangeEnd.setUTCMinutes(endMinutes);
    dragRef.current = null; setDragSelection(null);
    setEditor(editorFor(rangeStart, defaultMailboxId, timeZone, undefined, rangeEnd));
  };
  const cancelTimeDrag = () => { dragRef.current = null; setDragSelection(null); };
  const closeEventDetails = () => {
    if (!selected || detailsClosing) return;
    setDetailsClosing(true);
    detailsCloseTimer.current = window.setTimeout(() => {
      setSelected(null);
      setDetailsClosing(false);
      detailsCloseTimer.current = null;
    }, calendarMotionDelay(220));
  };
  const showEventDetails = (event: CalendarEvent) => {
    if (selected?.id === event.id && !detailsClosing) {
      closeEventDetails();
      return;
    }
    if (detailsCloseTimer.current != null) window.clearTimeout(detailsCloseTimer.current);
    if (sidebarCloseTimer.current != null) window.clearTimeout(sidebarCloseTimer.current);
    setDetailsClosing(false);
    setSidebarOpen(false);
    setSidebarClosing(false);
    setSelected(event);
  };
  const toggleSidebar = () => {
    if (sidebarOpen) {
      setSidebarOpen(false);
      setSidebarClosing(true);
      sidebarCloseTimer.current = window.setTimeout(() => {
        setSidebarClosing(false);
        sidebarCloseTimer.current = null;
      }, calendarMotionDelay(220));
      return;
    }
    if (sidebarCloseTimer.current != null) window.clearTimeout(sidebarCloseTimer.current);
    setSidebarClosing(false);
    setSidebarOpen(true);
  };
  const save = async (e: FormEvent) => {
    e.preventDefault(); if (!editor) return; setSaving(true); setSaveError("");
    const startAt = instantForWallDate(wallDateFromInput(editor.start), timeZone);
    const endAt = instantForWallDate(wallDateFromInput(editor.end), timeZone);
    const payload: CalendarEventPayload = { mailbox_id: editor.mailbox_id, title: editor.title, company: editor.company, role: editor.role, interviewer: editor.interviewer, work_model: editor.work_model, start_at: startAt.toISOString(), end_at: endAt.toISOString(), source_timezone: timeZone, description: editor.description, job_url: editor.job_url || null, meeting_type: editor.meeting_type, meeting_provider: editor.meeting_provider || null, meeting_url: editor.meeting_url || null, phone_number: editor.phone_number || null, phone_access_code: editor.phone_access_code || null, interview_status: "confirmation", application_status: editor.application_status };
    try { const saved = editor.id ? await updateCalendarEvent(editor.id, payload) : await createCalendarEvent(payload); setEvents((prev) => [...prev.filter((x) => x.id !== saved.id), saved]); showEventDetails(saved); setEditor(null); }
    catch (err) { setSaveError(err instanceof Error ? err.message : "Failed to save event"); }
    finally { setSaving(false); }
  };

  const copyMeetingLink = async (event: CalendarEvent) => {
    if (!event.meeting_url) return;
    try {
      await navigator.clipboard.writeText(event.meeting_url);
    } catch {
      const field = document.createElement("textarea");
      field.value = event.meeting_url;
      field.style.position = "fixed";
      field.style.opacity = "0";
      document.body.appendChild(field);
      field.select();
      document.execCommand("copy");
      field.remove();
    }
    setCopiedMeetingEventId(event.id);
    window.setTimeout(() => setCopiedMeetingEventId((current) => current === event.id ? null : current), 1800);
  };

  return <main className={`calendar-workspace ${sidebarOpen ? "sidebar-open" : ""} ${sidebarClosing ? "sidebar-closing" : ""} ${detailsClosing ? "details-closing" : ""}`}>
    <header className="calendar-toolbar">
      <div className="calendar-title-group"><button className="calendar-back" type="button" onClick={onClose} aria-label="Back to inbox">←</button><h1><strong>{new Intl.DateTimeFormat(undefined, { timeZone: "UTC", month: "long" }).format(anchor)}</strong><span>{anchor.getUTCFullYear()}</span></h1></div>
      <div className="calendar-nav"><button type="button" onClick={() => move(-1)} aria-label="Previous period">‹</button><button className="calendar-today" type="button" onClick={() => setAnchor(startOfDay(wallDateForInstant(new Date(), timeZone)))}>Today</button><button type="button" onClick={() => move(1)} aria-label="Next period">›</button></div>
      <div className="calendar-toolbar-actions"><CalendarTimeZonePicker value={timeZone} onChange={setTimeZone} /><CalendarViewPicker value={view} onChange={setView} /><button className="calendar-sidebar-toggle" type="button" aria-label={sidebarOpen ? "Hide calendar sidebar" : "Show calendar sidebar"} title={sidebarOpen ? "Hide sidebar" : "Show sidebar"} aria-pressed={sidebarOpen} onClick={toggleSidebar}>▥</button></div>
    </header>
    {error && <div className="calendar-state error"><span>{error}</span><button type="button" onClick={() => void load()}>Retry</button></div>}
    <div className={`calendar-main ${selected ? "has-details" : ""}`}>
      {view === "month" ? (
        <section className="calendar-grid month" aria-busy={loading}>
          {VISIBLE_WEEKDAYS.map((day) => <div className="calendar-weekday" key={day}>{day}</div>)}
        {days.map((day) => { const dayEvents = events.filter((event) => overlapsDay(event, day, timeZone)); const today = sameDay(day, nowInTimeZone); return <div className={`calendar-day ${today ? "today" : ""} ${day.getUTCMonth() !== anchor.getUTCMonth() ? "outside" : ""}`} key={day.toISOString()} onClick={() => openCreate(day)}>
            <div className="calendar-day-heading"><strong>{day.getUTCDate()}</strong>{today && <em>Today</em>}</div>
            <div className="calendar-day-events">{dayEvents.map((event) => <EventCard event={event} timeZone={timeZone} compact selected={selected?.id === event.id && !detailsClosing} activeHoverId={activeHoverId} onHoverActivate={activateEventHover} onHoverDeactivate={deactivateEventHover} onSelect={showEventDetails} key={event.id} />)}</div>
          </div>; })}
          {loading && <div className="calendar-loading">Loading calendar…</div>}
        </section>
      ) : (
        <section ref={timelineRef} className={`calendar-time-grid ${view}`} aria-busy={loading} aria-label={`${view} calendar. Drag on a time range to create an event.`} style={{ "--day-count": days.length, "--hour-height": `${hourHeight}px`, "--day-height": `${hourHeight * HOURS.length}px` } as React.CSSProperties}>
          <div ref={timelineHeaderRef} className="calendar-time-header">
            <div className="calendar-time-corner" />
            {days.map((day) => { const today = sameDay(day, nowInTimeZone); return <div className={`calendar-time-day-heading ${today ? "today" : ""}`} key={day.toISOString()}><span>{new Intl.DateTimeFormat(undefined, { timeZone: "UTC", weekday: "short" }).format(day)}</span><strong>{day.getUTCDate()}</strong></div>; })}
            <div className="calendar-all-day-label">all-day</div>
          {days.map((day) => <div className="calendar-all-day-cell" key={`all-day-${day.toISOString()}`}>{events.filter((event) => isAllDay(event, timeZone) && overlapsDay(event, day, timeZone)).map((event) => <EventCard event={event} timeZone={timeZone} compact selected={selected?.id === event.id && !detailsClosing} activeHoverId={activeHoverId} onHoverActivate={activateEventHover} onHoverDeactivate={deactivateEventHover} onSelect={showEventDetails} key={event.id} />)}</div>)}
          </div>
          <div className="calendar-time-body">
            <div className="calendar-time-axis">{HOURS.map((hour) => <span key={hour} style={{ top: `${hour * hourHeight}px` }}>{hourLabel(hour)}</span>)}</div>
            <div className="calendar-time-days">
            {days.map((day, dayIndex) => { const dayEvents = events.filter((event) => overlapsDay(event, day, timeZone)); const today = sameDay(day, nowInTimeZone); const nowMinutes = nowInTimeZone.getUTCHours() * 60 + nowInTimeZone.getUTCMinutes(); return <div
                className={`calendar-time-day ${today ? "today" : ""} ${defaultMailboxId == null ? "disabled" : ""}`}
                key={day.toISOString()}
                title={defaultMailboxId == null ? "Connect an email account to create events" : "Drag to create an event"}
                onPointerDown={(event) => beginTimeDrag(dayIndex, event)}
                onPointerMove={(event) => updateTimeDrag(dayIndex, event)}
                onPointerUp={(event) => finishTimeDrag(dayIndex, event)}
                onPointerCancel={cancelTimeDrag}
              >
                {HOURS.map((hour) => <div className="calendar-hour-slot" key={hour} />)}
                {today && <div className="calendar-now-line" style={{ top: `${(nowMinutes / 60) * hourHeight}px` }} />}
                {dragSelection?.dayIndex === dayIndex && <div className="calendar-drag-selection" style={{ top: `${(dragSelection.startMinutes / 60) * hourHeight}px`, height: `${((dragSelection.endMinutes - dragSelection.startMinutes) / 60) * hourHeight}px` }}><span>{formatWallTime(new Date(Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate(), 0, dragSelection.startMinutes)))} – {formatWallTime(new Date(Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate(), 0, dragSelection.endMinutes)))}</span></div>}
                {layoutTimedEvents(dayEvents, day, hourHeight, timeZone).map(({ event, position }) => <EventCard event={event} timeZone={timeZone} timed position={position} selected={selected?.id === event.id && !detailsClosing} activeHoverId={activeHoverId} onHoverActivate={activateEventHover} onHoverDeactivate={deactivateEventHover} onSelect={showEventDetails} key={event.id} />)}
              </div>; })}
            </div>
          </div>
          {loading && <div className="calendar-loading">Loading calendar…</div>}
        </section>
      )}
      {(selected || sidebarOpen || sidebarClosing) && <button className="calendar-panel-scrim" type="button" aria-label="Close calendar panel" onClick={selected ? closeEventDetails : toggleSidebar} />}
      {selected ? <aside className={`calendar-details ${detailsClosing ? "is-closing" : ""}`} aria-label="Event details" key={`details-${selected.id}`}>
        <header className="details-sticky">
          <button className="details-close" type="button" onClick={closeEventDetails} aria-label="Close event details"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 4 8 8M12 4l-8 8" /></svg></button>
          <div className="details-sticky-actions">
            {interviewType(selected).type === "video" && selected.meeting_url && <a className="details-primary-action" href={selected.meeting_url} target="_blank" rel="noreferrer"><MeetingTypeIcon type="video" /><span>Join meeting</span><svg className="details-action-arrow" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3h7v7M13 3 5 11" /></svg></a>}
            {interviewType(selected).type === "phone" && <span className="details-primary-action is-static"><MeetingTypeIcon type="phone" /><span>Phone call</span></span>}
          </div>
        </header>
        <div className="details-content">
          <dl className="details-list">
            <div className="details-row details-time-row"><dt>Interview time</dt><dd>{formatInterviewTimeRange(selected, timeZone)}</dd></div>
            <div className="details-row"><dt>Account</dt><dd>{selected.email_account}</dd></div>
            <div className="details-row"><dt>Company</dt><dd>{selected.company || "—"}</dd></div>
            <div className="details-row"><dt>Role</dt><dd>{selected.role || "—"}</dd></div>
            {selected.interview_status === "cancellation" && <div className="details-row details-canceled-row"><dt>Interview status</dt><dd><CanceledStatusIcon /><span>Canceled</span></dd></div>}
            <div className={`details-row details-status-row status-${selected.application_status.replaceAll("_", "-")}`}><dt>Application status</dt><dd><ApplicationStatusIcon status={selected.application_status} /><span>{applicationStatusLabel(selected)}</span></dd></div>
            {interviewType(selected).type === "video" && <div className="details-row details-action-row"><dt>Meeting</dt><dd>{meetingProviderLabel(selected) !== "—" ? <span className="details-meeting-value">{selected.meeting_url ? <a className="details-meeting-link" href={selected.meeting_url} target="_blank" rel="noreferrer" aria-label={`Open ${meetingProviderLabel(selected)}`} title={`Open ${meetingProviderLabel(selected)}`}><span className="details-meeting-icon"><MeetingTypeIcon type="video" /></span><span>{meetingProviderLabel(selected)}</span><svg className="details-meeting-arrow" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3h7v7M13 3 5 11" /></svg></a> : <span className="details-meeting-label"><span className="details-meeting-icon"><MeetingTypeIcon type="video" /></span><span>{meetingProviderLabel(selected)}</span></span>}{selected.meeting_url && <button className="details-copy-link details-copy-icon" type="button" onClick={() => void copyMeetingLink(selected)} aria-label={copiedMeetingEventId === selected.id ? "Meeting link copied" : `Copy ${meetingProviderLabel(selected)} meeting link`} title={copiedMeetingEventId === selected.id ? "Copied" : "Copy meeting link"}>{copiedMeetingEventId === selected.id ? <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 8.2 3 3 6-6.4" /></svg> : <svg viewBox="0 0 16 16" aria-hidden="true"><rect x="5.25" y="2.25" width="8.5" height="8.5" rx="1.5" /><path d="M10.75 10.75v1.5a1.5 1.5 0 0 1-1.5 1.5h-5.5a1.5 1.5 0 0 1-1.5-1.5v-5.5a1.5 1.5 0 0 1 1.5-1.5h1.5" /></svg>}</button>}</span> : "—"}</dd></div>}
          </dl>

          {selected.kind === "confirmed" && <div className="details-actions">
            {selected.kind === "confirmed" && <button className="details-secondary-action" type="button" onClick={() => setEditor(editorFor(new Date(selected.start_at), selected.mailbox_id, timeZone, selected))}><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m10.8 2.7 2.5 2.5-7.7 7.7-3 .5.5-3 7.7-7.7ZM9.4 4.1l2.5 2.5" /></svg>Edit event</button>}
          </div>}

          <section className="source-email">
            <div className="source-email-heading">
              <span className="source-email-icon"><svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2" y="3" width="12" height="10" rx="2" /><path d="m3 5 5 4 5-4" /></svg></span>
              <div><h3>Associated emails</h3><p>{emailLoading ? "Loading linked messages…" : `${emailMessages.length} linked message${emailMessages.length === 1 ? "" : "s"}`}</p></div>
            </div>
            {emailLoading && <div className="source-email-loading"><span />Loading emails…</div>}
            {emailError && <p className="error-text">{emailError}</p>}
            {!emailLoading && !emailError && emailMessages.length === 0 && <p className="source-email-empty">No emails are linked to this calendar item.</p>}
            {emailMessages.length > 0 && <ol className="source-email-list" aria-label="Emails associated with this calendar item, newest first">
              {emailMessages.map((message) => {
                const timestamp = message.received_at || message.created_at || selected.start_at;
                const expanded = expandedEmailId === message.id;
                return <li key={message.id}>
                  <button className="source-email-list-item" type="button" aria-expanded={expanded} onClick={() => setExpandedEmailId(expanded ? null : message.id)}>
                    <span className="source-email-list-top"><strong>{message.subject || "(no subject)"}</strong><time dateTime={timestamp}>{formatDateTime(timestamp, timeZone)}</time></span>
                    <span className="source-email-meta">{message.sender}{message.interview_subtype && <><span>·</span>{message.interview_subtype.replaceAll("_", " ")}</>}</span>
                    <span className="source-email-snippet">{message.snippet || message.body_text}</span>
                  </button>
                  {expanded && <div className="source-email-body"><EmailBody html={message.body_html} text={message.body_text} snippet={message.snippet} /></div>}
                </li>;
              })}
            </ol>}
          </section>
        </div>
      </aside> : <aside className="calendar-sidebar">
        <section className="calendar-mini-month">
          <header><strong>{new Intl.DateTimeFormat(undefined, { timeZone: "UTC", month: "long", year: "numeric" }).format(anchor)}</strong><div><button type="button" aria-label="Previous month" onClick={() => setAnchor(new Date(Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth() - 1, 1)))}>‹</button><button type="button" aria-label="Next month" onClick={() => setAnchor(new Date(Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth() + 1, 1)))}>›</button></div></header>
          <div className="mini-weekdays">{VISIBLE_WEEKDAYS.map((day) => <span key={day}>{day.slice(0, 2)}</span>)}</div>
          <div className="mini-days">{miniMonthDays.map((day) => <button type="button" className={`${day.getUTCMonth() !== anchor.getUTCMonth() ? "outside" : ""} ${view === "week" && day >= start && day < end ? "in-range" : ""} ${sameDay(day, anchor) ? "selected" : ""} ${sameDay(day, nowInTimeZone) ? "today" : ""}`} key={day.toISOString()} onClick={() => setAnchor(day)}>{day.getUTCDate()}</button>)}</div>
        </section>
        <section className="calendar-next-meeting"><h2>Next meeting</h2>{nextMeeting ? <button type="button" className="next-meeting-card" onClick={() => showEventDetails(nextMeeting)}><span className="next-meeting-logo" style={{ "--account-color": accountColor(nextMeeting.mailbox_id) } as React.CSSProperties}>{eventInitials(nextMeeting)}</span><span><strong>{nextMeeting.title}</strong><small>{startsIn(nextMeeting.start_at)}</small></span></button> : <p>No upcoming meetings in this view.</p>}</section>
        <ScheduledInterviewChart accounts={scheduledInterviewsByAccount} />
        <section className="calendar-legend"><h2>Calendars</h2>{mailboxes.filter((mailbox) => mailbox.is_active).map((mailbox) => <div key={mailbox.id}><span style={{ background: accountColor(mailbox.id) }} /><p title={mailbox.email_address}>{mailbox.email_address}</p></div>)}</section>
      </aside>}
    </div>
    {editor && <div className="modal-backdrop" role="presentation" onMouseDown={() => !saving && setEditor(null)}><form className="calendar-editor" onSubmit={save} onMouseDown={(e) => e.stopPropagation()}><header><h2>{editor.id ? "Edit event" : "New event"}</h2><button type="button" onClick={() => setEditor(null)}>×</button></header>
      <label>Email account<CompactSelect value={editor.mailbox_id} onChange={(e) => setEditor({ ...editor, mailbox_id: Number(e.target.value) })} required>{mailboxes.filter((m) => m.is_active).map((m) => <option value={m.id} key={m.id}>{m.email_address}</option>)}</CompactSelect></label>
      <label>Title<input required value={editor.title} onChange={(e) => setEditor({ ...editor, title: e.target.value })} /></label><div className="editor-row"><label>Company<input value={editor.company} onChange={(e) => setEditor({ ...editor, company: e.target.value })} /></label><label>Role<input value={editor.role} onChange={(e) => setEditor({ ...editor, role: e.target.value })} /></label></div>
      <div className="editor-row"><label>Interviewer<input value={editor.interviewer} onChange={(e) => setEditor({ ...editor, interviewer: e.target.value })} placeholder="Name or email" /></label><label>Work model<CompactSelect value={editor.work_model} onChange={(e) => setEditor({ ...editor, work_model: e.target.value as Editor["work_model"] })}><option value="unspecified">Not specified</option><option value="remote">Remote</option><option value="hybrid">Hybrid</option><option value="on_site">On-site</option></CompactSelect></label></div>
      <div className="editor-timezone">Times shown in {timeZone.replaceAll("_", " ")}</div><div className="editor-row"><ModernDateTimePicker label="Start" value={editor.start} minuteStep={5} onChange={(start) => setEditor((current) => {
        if (!current) return current;
        if (current.end > start) return { ...current, start };
        const nextEnd = wallDateFromInput(start);
        nextEnd.setUTCMinutes(nextEnd.getUTCMinutes() + DEFAULT_EVENT_DURATION_MINUTES);
        return { ...current, start, end: localInput(nextEnd) };
      })} /><ModernDateTimePicker label="End" value={editor.end} min={editor.start} minuteStep={5} onChange={(end) => setEditor((current) => current ? { ...current, end } : current)} /></div>
      <label>Interview type<CompactSelect value={editor.meeting_type} onChange={(e) => setEditor({ ...editor, meeting_type: e.target.value as Editor["meeting_type"] })}><option value="video">Video call</option><option value="phone">Phone call</option></CompactSelect></label>
      {editor.meeting_type === "video" && <div className="editor-row"><label>Provider<input placeholder="Zoom, Google Meet…" value={editor.meeting_provider} onChange={(e) => setEditor({ ...editor, meeting_provider: e.target.value })} /></label><label>HTTPS meeting link<input type="url" value={editor.meeting_url} onChange={(e) => setEditor({ ...editor, meeting_url: e.target.value })} /></label></div>}
      {editor.meeting_type === "phone" && <div className="editor-row"><label>Phone<input type="tel" value={editor.phone_number} onChange={(e) => setEditor({ ...editor, phone_number: e.target.value })} /></label><label>Access code<input value={editor.phone_access_code} onChange={(e) => setEditor({ ...editor, phone_access_code: e.target.value })} /></label></div>}
      <label>Job link<input type="url" value={editor.job_url} onChange={(e) => setEditor({ ...editor, job_url: e.target.value })} /></label><label>Description<textarea rows={4} value={editor.description} onChange={(e) => setEditor({ ...editor, description: e.target.value })} /></label>{saveError && <p className="error-text">{saveError}</p>}<footer><button type="button" onClick={() => setEditor(null)}>Cancel</button><button className="primary" disabled={saving}>{saving ? "Saving…" : "Save event"}</button></footer>
    </form></div>}
  </main>;
}
