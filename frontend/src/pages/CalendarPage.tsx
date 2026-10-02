import { FormEvent, KeyboardEvent as ReactKeyboardEvent, MouseEvent, PointerEvent as ReactPointerEvent, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import EmailBody from "../components/EmailBody";
import CompactSelect from "../components/CompactSelect";
import {
  createCalendarEvent,
  eventsUrl,
  fetchCalendarEvents,
  fetchEmailDetail,
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
const CALENDAR_TIME_ZONE_KEY = "calendar-time-zone";
const SYSTEM_TIME_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
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
function startOfWeek(date: Date) { const out = startOfDay(date); out.setUTCDate(out.getUTCDate() - out.getUTCDay()); return out; }
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
  return addDays(first, -first.getUTCDay());
}
function localInput(date: Date) {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}T${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
}
function formatTime(value: string, timeZone: string, showZone = false) { return new Intl.DateTimeFormat(undefined, { timeZone, hour: "numeric", minute: "2-digit", ...(showZone ? { timeZoneName: "short" as const } : {}) }).format(new Date(value)); }
function formatWallTime(value: Date) { return new Intl.DateTimeFormat(undefined, { timeZone: "UTC", hour: "numeric", minute: "2-digit" }).format(value); }
function formatDateTime(value: string, timeZone: string) { return new Intl.DateTimeFormat(undefined, { timeZone, month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" }).format(new Date(value)); }
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

function EventCard({ event, timeZone, compact, timed, position, onSelect }: { event: CalendarEvent; timeZone: string; compact?: boolean; timed?: boolean; position?: React.CSSProperties; onSelect: (event: CalendarEvent) => void }) {
  const color = accountColor(event.mailbox_id);
  const canceled = event.interview_status === "cancellation";
  const action = event.meeting_type === "video" ? "🎥" : event.meeting_type === "phone" ? "☎" : "";
  const launch = (e: MouseEvent) => {
    e.stopPropagation();
    if (event.meeting_url) window.open(event.meeting_url, "_blank", "noopener,noreferrer");
    else if (event.phone_number) window.location.href = `tel:${event.phone_number.replace(/[^+\d]/g, "")}`;
  };
  return <button type="button" className={`calendar-event ${event.kind} ${canceled ? "canceled" : ""} ${timed ? "timed" : ""}`} style={{ "--account-color": color, ...position } as React.CSSProperties} onPointerDown={(e) => e.stopPropagation()} onClick={(e) => { e.stopPropagation(); onSelect(event); }} title={`${event.email_account}\n${event.title}`}>
    <span className="calendar-event-main"><span className="calendar-event-time">{formatTime(event.start_at, timeZone)}{timed && ` – ${formatTime(event.end_at, timeZone)}`}</span> {event.kind === "availability" ? "Available" : event.title}</span>
    {!compact && (event.company || event.role) && <span className="calendar-event-meta">{[event.company, event.role].filter(Boolean).join(" · ")}</span>}
    <span className="calendar-event-badges">
      {event.application_status === "rejected" && <span className="event-badge danger">Rejected</span>}
      {event.application_status === "position_closed" && <span className="event-badge warning">Position Closed</span>}
      {action && <span className="event-access" onClick={launch} role="link" tabIndex={0} title={event.meeting_type === "video" ? "Join meeting" : "Call"}>{action}{!compact && <span>{event.meeting_type === "video" ? " Join" : " Call"}</span>}</span>}
    </span>
  </button>;
}

type Editor = { id?: number; mailbox_id: number; title: string; company: string; role: string; start: string; end: string; description: string; job_url: string; meeting_type: "video" | "phone" | "in_person" | "unspecified"; meeting_provider: string; meeting_url: string; phone_number: string; phone_access_code: string };
function editorFor(date: Date, mailboxId: number, timeZone: string, event?: CalendarEvent, selectedEnd?: Date): Editor {
  const start = event ? wallDateForInstant(new Date(event.start_at), timeZone) : selectedEnd ? new Date(date) : new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate(), 9));
  const end = event ? wallDateForInstant(new Date(event.end_at), timeZone) : selectedEnd ?? new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), start.getUTCDate(), start.getUTCHours() + 1, start.getUTCMinutes()));
  return { id: event?.id, mailbox_id: event?.mailbox_id ?? mailboxId, title: event?.title ?? "Interview", company: event?.company ?? "", role: event?.role ?? "", start: localInput(start), end: localInput(end), description: event?.description ?? "", job_url: event?.job_url ?? "", meeting_type: event?.meeting_type ?? "unspecified", meeting_provider: event?.meeting_provider ?? "", meeting_url: event?.meeting_url ?? "", phone_number: event?.phone_number ?? "", phone_access_code: event?.phone_access_code ?? "" };
}

type DragSelection = { dayIndex: number; startMinutes: number; endMinutes: number };

export default function CalendarPage({ mailboxes, onClose, onOpenEmail }: { mailboxes: Mailbox[]; onClose: () => void; onOpenEmail: (id: number, mailboxId: number) => void }) {
  const [view, setView] = useState<ViewMode>(() => (localStorage.getItem("calendar-view") as ViewMode) || "week");
  const [timeZone, setTimeZone] = useState(loadCalendarTimeZone);
  const [anchor, setAnchor] = useState(() => wallDateForInstant(new Date(), timeZone));
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [loading, setLoading] = useState(true); const [error, setError] = useState("");
  const [selected, setSelected] = useState<CalendarEvent | null>(null);
  const [meetWith, setMeetWith] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [email, setEmail] = useState<EmailDetail | null>(null); const [emailLoading, setEmailLoading] = useState(false); const [emailError, setEmailError] = useState(""); const [emailExpanded, setEmailExpanded] = useState(false);
  const [editor, setEditor] = useState<Editor | null>(null); const [saving, setSaving] = useState(false); const [saveError, setSaveError] = useState("");
  const [dragSelection, setDragSelection] = useState<DragSelection | null>(null);
  const [hourHeight, setHourHeight] = useState(HOUR_HEIGHT);
  const refreshRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
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
    try { const rows = await fetchCalendarEvents(instantForWallDate(start, timeZone).toISOString(), instantForWallDate(end, timeZone).toISOString(), controller.signal); if (request === refreshRef.current) setEvents(rows); }
    catch (err) { if (request === refreshRef.current && (err as Error).name !== "AbortError") setError(err instanceof Error ? err.message : "Failed to load calendar"); }
    finally { if (request === refreshRef.current) setLoading(false); }
  }, [start.getTime(), end.getTime(), timeZone]);
  useEffect(() => { void load(); return () => abortRef.current?.abort(); }, [load]);
  useEffect(() => { const source = new EventSource(eventsUrl()); source.addEventListener("calendar.changed", () => void load()); return () => source.close(); }, [load]);
  useEffect(() => { localStorage.setItem("calendar-view", view); }, [view]);
  useEffect(() => { try { localStorage.setItem(CALENDAR_TIME_ZONE_KEY, timeZone); } catch { /* ignore unavailable storage */ } }, [timeZone]);
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
    setEmail(null); setEmailError(""); setEmailExpanded(false);
    if (!selected?.source_email_id) return;
    let active = true; setEmailLoading(true);
    fetchEmailDetail(selected.source_email_id, false).then((value) => active && setEmail(value)).catch((err) => active && setEmailError(err instanceof Error ? err.message : "Failed to load email")).finally(() => active && setEmailLoading(false));
    return () => { active = false; };
  }, [selected?.id]);

  const visible = useMemo(() => { const query = meetWith.trim().toLowerCase(); return query ? events.filter((event) => [event.title, event.company, event.role, event.email_account].some((value) => value?.toLowerCase().includes(query))) : events; }, [events, meetWith]);
  const days = useMemo(() => Array.from({ length: view === "month" ? 42 : view === "week" ? 7 : 1 }, (_, i) => addDays(start, i)), [start, view]);
  const miniMonthStart = useMemo(() => { const first = new Date(Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth(), 1)); return addDays(first, -first.getUTCDay()); }, [anchor.getUTCFullYear(), anchor.getUTCMonth()]);
  const miniMonthDays = useMemo(() => Array.from({ length: 42 }, (_, i) => addDays(miniMonthStart, i)), [miniMonthStart.getTime()]);
  const nextMeeting = useMemo(() => visible.filter((event) => event.kind === "confirmed" && event.interview_status !== "cancellation" && new Date(event.end_at).getTime() > Date.now()).sort((a, b) => new Date(a.start_at).getTime() - new Date(b.start_at).getTime())[0] ?? null, [visible]);
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
    setDragSelection({ dayIndex, startMinutes: anchorMinutes, endMinutes: anchorMinutes + SNAP_MINUTES });
  };
  const updateTimeDrag = (dayIndex: number, event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.dayIndex !== dayIndex) return;
    const currentMinutes = minutesAtPointer(event.currentTarget, event.clientY);
    setDragSelection({ dayIndex, startMinutes: Math.min(drag.anchorMinutes, currentMinutes), endMinutes: drag.anchorMinutes === currentMinutes ? currentMinutes + SNAP_MINUTES : Math.max(drag.anchorMinutes, currentMinutes) });
  };
  const finishTimeDrag = (dayIndex: number, event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.dayIndex !== dayIndex || defaultMailboxId == null) return;
    const currentMinutes = minutesAtPointer(event.currentTarget, event.clientY);
    const startMinutes = Math.min(drag.anchorMinutes, currentMinutes);
    const endMinutes = drag.anchorMinutes === currentMinutes ? Math.min(DAY_MINUTES, startMinutes + 60) : Math.max(drag.anchorMinutes, currentMinutes);
    const rangeStart = new Date(days[dayIndex]); rangeStart.setUTCMinutes(startMinutes);
    const rangeEnd = new Date(days[dayIndex]); rangeEnd.setUTCMinutes(endMinutes);
    dragRef.current = null; setDragSelection(null);
    setEditor(editorFor(rangeStart, defaultMailboxId, timeZone, undefined, rangeEnd));
  };
  const cancelTimeDrag = () => { dragRef.current = null; setDragSelection(null); };
  const save = async (e: FormEvent) => {
    e.preventDefault(); if (!editor) return; setSaving(true); setSaveError("");
    const startAt = instantForWallDate(wallDateFromInput(editor.start), timeZone);
    const endAt = instantForWallDate(wallDateFromInput(editor.end), timeZone);
    const payload: CalendarEventPayload = { mailbox_id: editor.mailbox_id, title: editor.title, company: editor.company, role: editor.role, start_at: startAt.toISOString(), end_at: endAt.toISOString(), source_timezone: timeZone, description: editor.description, job_url: editor.job_url || null, meeting_type: editor.meeting_type, meeting_provider: editor.meeting_provider || null, meeting_url: editor.meeting_url || null, phone_number: editor.phone_number || null, phone_access_code: editor.phone_access_code || null, interview_status: "confirmation", application_status: "active" };
    try { const saved = editor.id ? await updateCalendarEvent(editor.id, payload) : await createCalendarEvent(payload); setEvents((prev) => [...prev.filter((x) => x.id !== saved.id), saved]); setSelected(saved); setEditor(null); }
    catch (err) { setSaveError(err instanceof Error ? err.message : "Failed to save event"); }
    finally { setSaving(false); }
  };

  return <main className={`calendar-workspace ${sidebarOpen ? "sidebar-open" : ""}`}>
    <header className="calendar-toolbar">
      <div className="calendar-title-group"><button className="calendar-back" type="button" onClick={onClose} aria-label="Back to inbox">←</button><h1><strong>{new Intl.DateTimeFormat(undefined, { timeZone: "UTC", month: "long" }).format(anchor)}</strong><span>{anchor.getUTCFullYear()}</span></h1></div>
      <div className="calendar-nav"><button type="button" onClick={() => move(-1)} aria-label="Previous period">‹</button><button className="calendar-today" type="button" onClick={() => setAnchor(startOfDay(wallDateForInstant(new Date(), timeZone)))}>Today</button><button type="button" onClick={() => move(1)} aria-label="Next period">›</button></div>
      <div className="calendar-toolbar-actions"><CalendarTimeZonePicker value={timeZone} onChange={setTimeZone} /><CalendarViewPicker value={view} onChange={setView} /><button className="calendar-sidebar-toggle" type="button" aria-label="Toggle calendar sidebar" aria-pressed={sidebarOpen} onClick={() => setSidebarOpen((open) => !open)}>▥</button></div>
    </header>
    {error && <div className="calendar-state error"><span>{error}</span><button type="button" onClick={() => void load()}>Retry</button></div>}
    <div className={`calendar-main ${selected ? "has-details" : ""}`}>
      {view === "month" ? (
        <section className="calendar-grid month" aria-busy={loading}>
          {["Sun","Mon","Tue","Wed","Thu","Fri","Sat"].map((day) => <div className="calendar-weekday" key={day}>{day}</div>)}
          {days.map((day) => { const dayEvents = visible.filter((event) => overlapsDay(event, day, timeZone)); const today = sameDay(day, nowInTimeZone); return <div className={`calendar-day ${today ? "today" : ""} ${day.getUTCMonth() !== anchor.getUTCMonth() ? "outside" : ""}`} key={day.toISOString()} onClick={() => openCreate(day)}>
            <div className="calendar-day-heading"><strong>{day.getUTCDate()}</strong>{today && <em>Today</em>}</div>
            <div className="calendar-day-events">{dayEvents.map((event) => <EventCard event={event} timeZone={timeZone} compact onSelect={setSelected} key={event.id} />)}</div>
          </div>; })}
          {loading && <div className="calendar-loading">Loading calendar…</div>}
        </section>
      ) : (
        <section ref={timelineRef} className={`calendar-time-grid ${view}`} aria-busy={loading} aria-label={`${view} calendar. Drag on a time range to create an event.`} style={{ "--day-count": days.length, "--hour-height": `${hourHeight}px`, "--day-height": `${hourHeight * HOURS.length}px` } as React.CSSProperties}>
          <div ref={timelineHeaderRef} className="calendar-time-header">
            <div className="calendar-time-corner" />
            {days.map((day) => { const today = sameDay(day, nowInTimeZone); return <div className={`calendar-time-day-heading ${today ? "today" : ""}`} key={day.toISOString()}><span>{new Intl.DateTimeFormat(undefined, { timeZone: "UTC", weekday: "short" }).format(day)}</span><strong>{day.getUTCDate()}</strong></div>; })}
            <div className="calendar-all-day-label">all-day</div>
            {days.map((day) => <div className="calendar-all-day-cell" key={`all-day-${day.toISOString()}`}>{visible.filter((event) => isAllDay(event, timeZone) && overlapsDay(event, day, timeZone)).map((event) => <EventCard event={event} timeZone={timeZone} compact onSelect={setSelected} key={event.id} />)}</div>)}
          </div>
          <div className="calendar-time-body">
            <div className="calendar-time-axis">{HOURS.map((hour) => <span key={hour} style={{ top: `${hour * hourHeight}px` }}>{hourLabel(hour)}</span>)}</div>
            <div className="calendar-time-days">
              {days.map((day, dayIndex) => { const dayEvents = visible.filter((event) => overlapsDay(event, day, timeZone)); const today = sameDay(day, nowInTimeZone); const nowMinutes = nowInTimeZone.getUTCHours() * 60 + nowInTimeZone.getUTCMinutes(); return <div
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
                {layoutTimedEvents(dayEvents, day, hourHeight, timeZone).map(({ event, position }) => <EventCard event={event} timeZone={timeZone} timed position={position} onSelect={setSelected} key={event.id} />)}
              </div>; })}
            </div>
          </div>
          {loading && <div className="calendar-loading">Loading calendar…</div>}
        </section>
      )}
      {selected ? <aside className="calendar-details">
        <div className="details-sticky"><button type="button" onClick={() => setSelected(null)} aria-label="Close details">×</button><div>{selected.meeting_url && <a className="primary" href={selected.meeting_url} target="_blank" rel="noreferrer">Join meeting</a>}{!selected.meeting_url && selected.phone_number && <a className="primary" href={`tel:${selected.phone_number.replace(/[^+\d]/g, "")}`}>Call</a>}</div></div>
        <div className="details-content"><span className={`details-kind ${selected.kind}`}>{selected.kind === "confirmed" ? "Scheduled interview" : "Available time"}</span><h2>{selected.title}</h2>
          <dl><dt>Account</dt><dd>{selected.email_account}</dd><dt>Date and time</dt><dd>{formatDateTime(selected.start_at, timeZone)} – {formatTime(selected.end_at, timeZone, true)}</dd><dt>Company</dt><dd>{selected.company || "—"}</dd><dt>Role</dt><dd>{selected.role || "—"}</dd><dt>Interview status</dt><dd>{selected.interview_status?.replaceAll("_", " ") || "—"}</dd><dt>Application outcome</dt><dd className={selected.application_status !== "active" ? "status-danger" : ""}>{selected.application_status.replaceAll("_", " ")}</dd>{selected.meeting_provider && <><dt>Meeting</dt><dd>{selected.meeting_provider}{selected.phone_access_code ? ` · Code ${selected.phone_access_code}` : ""}</dd></>}</dl>
          {selected.job_url && <a href={selected.job_url} target="_blank" rel="noreferrer">View job posting ↗</a>}{selected.description && <p className="details-description">{selected.description}</p>}
          {selected.kind === "confirmed" && <button type="button" onClick={() => setEditor(editorFor(new Date(selected.start_at), selected.mailbox_id, timeZone, selected))}>Edit event</button>}
          {selected.source_email_id && <section className="source-email"><h3>Source email</h3>{emailLoading && <p>Loading email…</p>}{emailError && <p className="error-text">{emailError}</p>}{email && <><strong>{email.subject}</strong><p>{email.sender} · {formatDateTime(email.received_at || email.created_at || selected.start_at, timeZone)} · {selected.email_account}</p><p>{email.snippet}</p><div className="source-email-actions"><button type="button" onClick={() => setEmailExpanded((v) => !v)}>{emailExpanded ? "Hide body" : "Show body"}</button><button type="button" onClick={() => onOpenEmail(email.id, email.mailbox_id)}>Open in Inbox</button></div>{emailExpanded && <div className="source-email-body"><EmailBody html={email.body_html} text={email.body_text} snippet={email.snippet} /></div>}</>}</section>}
        </div>
      </aside> : <aside className="calendar-sidebar">
        <section className="calendar-mini-month">
          <header><strong>{new Intl.DateTimeFormat(undefined, { timeZone: "UTC", month: "long", year: "numeric" }).format(anchor)}</strong><div><button type="button" aria-label="Previous month" onClick={() => setAnchor(new Date(Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth() - 1, 1)))}>‹</button><button type="button" aria-label="Next month" onClick={() => setAnchor(new Date(Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth() + 1, 1)))}>›</button></div></header>
          <div className="mini-weekdays">{["Su","Mo","Tu","We","Th","Fr","Sa"].map((day) => <span key={day}>{day}</span>)}</div>
          <div className="mini-days">{miniMonthDays.map((day) => <button type="button" className={`${day.getUTCMonth() !== anchor.getUTCMonth() ? "outside" : ""} ${view === "week" && day >= start && day < end ? "in-range" : ""} ${sameDay(day, anchor) ? "selected" : ""} ${sameDay(day, nowInTimeZone) ? "today" : ""}`} key={day.toISOString()} onClick={() => setAnchor(day)}>{day.getUTCDate()}</button>)}</div>
        </section>
        <section className="calendar-next-meeting"><h2>Next meeting</h2>{nextMeeting ? <button type="button" className="next-meeting-card" onClick={() => setSelected(nextMeeting)}><span className="next-meeting-logo" style={{ "--account-color": accountColor(nextMeeting.mailbox_id) } as React.CSSProperties}>{eventInitials(nextMeeting)}</span><span><strong>{nextMeeting.title}</strong><small>{startsIn(nextMeeting.start_at)}</small></span></button> : <p>No upcoming meetings in this view.</p>}</section>
        <section className="calendar-meet-with"><h2>Meet with</h2><input value={meetWith} onChange={(event) => setMeetWith(event.target.value)} placeholder="Add participants" list="calendar-meet-options" aria-label="Filter calendar by person, company, or account" /><datalist id="calendar-meet-options">{mailboxes.filter((mailbox) => mailbox.is_active).map((mailbox) => <option value={mailbox.email_address} key={mailbox.id} />)}{events.filter((event) => event.company).map((event) => <option value={event.company} key={`company-${event.id}`} />)}</datalist></section>
        <section className="calendar-legend"><h2>Calendars</h2>{mailboxes.filter((mailbox) => mailbox.is_active).map((mailbox) => <div key={mailbox.id}><span style={{ background: accountColor(mailbox.id) }} /><p title={mailbox.email_address}>{mailbox.email_address}</p></div>)}</section>
      </aside>}
    </div>
    {editor && <div className="modal-backdrop" role="presentation" onMouseDown={() => !saving && setEditor(null)}><form className="calendar-editor" onSubmit={save} onMouseDown={(e) => e.stopPropagation()}><header><h2>{editor.id ? "Edit event" : "New event"}</h2><button type="button" onClick={() => setEditor(null)}>×</button></header>
      <label>Email account<CompactSelect value={editor.mailbox_id} onChange={(e) => setEditor({ ...editor, mailbox_id: Number(e.target.value) })} required>{mailboxes.filter((m) => m.is_active).map((m) => <option value={m.id} key={m.id}>{m.email_address}</option>)}</CompactSelect></label>
      <label>Title<input required value={editor.title} onChange={(e) => setEditor({ ...editor, title: e.target.value })} /></label><div className="editor-row"><label>Company<input value={editor.company} onChange={(e) => setEditor({ ...editor, company: e.target.value })} /></label><label>Role<input value={editor.role} onChange={(e) => setEditor({ ...editor, role: e.target.value })} /></label></div>
      <div className="editor-timezone">Times shown in {timeZone.replaceAll("_", " ")}</div><div className="editor-row"><label>Start<input type="datetime-local" required value={editor.start} onChange={(e) => setEditor({ ...editor, start: e.target.value })} /></label><label>End<input type="datetime-local" required value={editor.end} onChange={(e) => setEditor({ ...editor, end: e.target.value })} /></label></div>
      <label>Meeting type<CompactSelect value={editor.meeting_type} onChange={(e) => setEditor({ ...editor, meeting_type: e.target.value as Editor["meeting_type"] })}><option value="unspecified">Unspecified</option><option value="video">Video</option><option value="phone">Phone</option><option value="in_person">In person</option></CompactSelect></label>
      {editor.meeting_type === "video" && <div className="editor-row"><label>Provider<input placeholder="Zoom, Google Meet…" value={editor.meeting_provider} onChange={(e) => setEditor({ ...editor, meeting_provider: e.target.value })} /></label><label>HTTPS meeting link<input type="url" value={editor.meeting_url} onChange={(e) => setEditor({ ...editor, meeting_url: e.target.value })} /></label></div>}
      {editor.meeting_type === "phone" && <div className="editor-row"><label>Phone<input type="tel" value={editor.phone_number} onChange={(e) => setEditor({ ...editor, phone_number: e.target.value })} /></label><label>Access code<input value={editor.phone_access_code} onChange={(e) => setEditor({ ...editor, phone_access_code: e.target.value })} /></label></div>}
      <label>Job link<input type="url" value={editor.job_url} onChange={(e) => setEditor({ ...editor, job_url: e.target.value })} /></label><label>Description<textarea rows={4} value={editor.description} onChange={(e) => setEditor({ ...editor, description: e.target.value })} /></label>{saveError && <p className="error-text">{saveError}</p>}<footer><button type="button" onClick={() => setEditor(null)}>Cancel</button><button className="primary" disabled={saving}>{saving ? "Saving…" : "Save event"}</button></footer>
    </form></div>}
  </main>;
}
