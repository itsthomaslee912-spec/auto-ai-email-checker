import { FormEvent, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent, memo, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import EmailBody from "../components/EmailBody";
import CompactSelect from "../components/CompactSelect";
import ModernDateTimePicker from "../components/ModernDateTimePicker";
import { accountColor, accountInk, defaultAccountColor } from "../accountColor";
import {
  createCalendarEvent,
  deleteCalendarEvent,
  openEvents,
  fetchCalendarEvents,
  fetchCalendarEventEmails,
  fetchEmailDetail,
  fetchPendingInterviews,
  peekPendingInterviews,
  updateCalendarEvent,
  type CalendarEvent,
  type CalendarEventPayload,
  type EmailDetail,
  type Mailbox,
  type PendingInterview,
} from "../api";

type ViewMode = "month" | "week" | "day";
type CalendarStatusFilter = "active" | "passed" | "canceled" | "rejected_closed" | "available";

const VIEW_OPTIONS: { value: ViewMode; label: string }[] = [
  { value: "month", label: "Month" },
  { value: "week", label: "Week" },
  { value: "day", label: "Day" },
];

const HOURS = Array.from({ length: 24 }, (_, hour) => hour);
const HOUR_HEIGHT = 64;
const DAY_MINUTES = 24 * 60;
const SNAP_MINUTES = 15;
const DEFAULT_EVENT_DURATION_MINUTES = 30;
const VISIBLE_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
function decodeMailText(value: string) {
  return value
    .replace(/&#(\d+);/g, (_, code) => String.fromCodePoint(Number(code)))
    .replace(/&#x([0-9a-f]+);/gi, (_, code) => String.fromCodePoint(parseInt(code, 16)))
    .replace(/&apos;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

const CALENDAR_TIME_ZONE_KEY = "calendar-time-zone";
const CALENDAR_STATUS_VIEW_KEY = "calendar-status-view";
const CALENDAR_PANEL_WIDTH_KEY = "calendar-panel-width";
const CALENDAR_PANEL_MIN = 352;
const CALENDAR_PANEL_MAX = 640;
const CALENDAR_BOARD_MIN = 420;
const CALENDAR_RANGE_CACHE = new Map<string, CalendarEvent[]>();
const CALENDAR_RANGE_INFLIGHT = new Map<string, Promise<CalendarEvent[]>>();
const CALENDAR_RANGE_CACHE_LIMIT = 12;
const CALENDAR_STATUS_FILTERS: { value: CalendarStatusFilter; label: string }[] = [
  { value: "active", label: "Booked" },
  { value: "passed", label: "Passed" },
  { value: "canceled", label: "Canceled" },
  { value: "rejected_closed", label: "Rejected/Closed" },
  { value: "available", label: "Available" },
];
const SYSTEM_TIME_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const calendarMotionDelay = (duration: number) => typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? 0 : duration;
const defaultCalendarPanelWidth = () => Math.round(Math.min(512, Math.max(CALENDAR_PANEL_MIN, window.innerWidth * .32)));
const loadCalendarPanelWidth = () => {
  try {
    const value = Number(localStorage.getItem(CALENDAR_PANEL_WIDTH_KEY));
    if (Number.isFinite(value) && value >= CALENDAR_PANEL_MIN) return Math.min(CALENDAR_PANEL_MAX, Math.round(value));
  } catch { /* ignore unavailable storage */ }
  return defaultCalendarPanelWidth();
};
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
type UsHoliday = { id: string; name: string; date: Date; observed: boolean };
function nthWeekdayOfMonth(year: number, month: number, weekday: number, occurrence: number) {
  const first = new Date(Date.UTC(year, month, 1));
  return new Date(Date.UTC(year, month, 1 + ((weekday - first.getUTCDay() + 7) % 7) + (occurrence - 1) * 7));
}
function lastWeekdayOfMonth(year: number, month: number, weekday: number) {
  const last = new Date(Date.UTC(year, month + 1, 0));
  return addDays(last, -((last.getUTCDay() - weekday + 7) % 7));
}
function usFederalHolidaysForYear(year: number): UsHoliday[] {
  const holidays: UsHoliday[] = [];
  const add = (name: string, date: Date, observed = false) => holidays.push({
    id: `${date.toISOString().slice(0, 10)}-${name}-${observed ? "observed" : "actual"}`,
    name: observed ? `${name} (observed)` : name,
    date,
    observed,
  });
  const fixed = (name: string, month: number, day: number) => {
    const date = new Date(Date.UTC(year, month, day));
    add(name, date);
    if (date.getUTCDay() === 6) add(name, addDays(date, -1), true);
    if (date.getUTCDay() === 0) add(name, addDays(date, 1), true);
  };
  fixed("New Year's Day", 0, 1);
  add("Birthday of Martin Luther King, Jr.", nthWeekdayOfMonth(year, 0, 1, 3));
  add("Washington's Birthday", nthWeekdayOfMonth(year, 1, 1, 3));
  add("Memorial Day", lastWeekdayOfMonth(year, 4, 1));
  fixed("Juneteenth National Independence Day", 5, 19);
  fixed("Independence Day", 6, 4);
  add("Labor Day", nthWeekdayOfMonth(year, 8, 1, 1));
  add("Columbus Day", nthWeekdayOfMonth(year, 9, 1, 2));
  fixed("Veterans Day", 10, 11);
  add("Thanksgiving Day", nthWeekdayOfMonth(year, 10, 4, 4));
  fixed("Christmas Day", 11, 25);
  return holidays;
}
function usFederalHolidays(start: Date, end: Date) {
  const firstYear = start.getUTCFullYear() - 1;
  const lastYear = end.getUTCFullYear() + 1;
  const unique = new Map<string, UsHoliday>();
  for (let year = firstYear; year <= lastYear; year += 1) {
    for (const holiday of usFederalHolidaysForYear(year)) {
      if (holiday.date < start || holiday.date >= end) continue;
      unique.set(holiday.id, holiday);
    }
  }
  return [...unique.values()].sort((a, b) => a.date.getTime() - b.date.getTime() || a.name.localeCompare(b.name));
}
function endExclusive(start: Date, view: ViewMode) {
  if (view === "day") return addDays(startOfDay(start), 1);
  if (view === "week") return addDays(startOfWeek(start), 7);
  const followingMonth = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + 1, 1));
  return addDays(followingMonth, 7 - followingMonth.getUTCDay());
}
function rememberCalendarRange(key: string, rows: CalendarEvent[]) {
  CALENDAR_RANGE_CACHE.set(key, rows);
  if (CALENDAR_RANGE_CACHE.size <= CALENDAR_RANGE_CACHE_LIMIT) return;
  const oldest = CALENDAR_RANGE_CACHE.keys().next().value;
  if (oldest) CALENDAR_RANGE_CACHE.delete(oldest);
}

function calendarRangeKey(start: Date, end: Date, timeZone: string) {
  return `${start.getTime()}|${end.getTime()}|${timeZone}`;
}

function fetchCalendarRange(start: Date, end: Date, timeZone: string, signal?: AbortSignal, forceFresh = false): Promise<CalendarEvent[]> {
  const key = calendarRangeKey(start, end, timeZone);
  const existing = CALENDAR_RANGE_INFLIGHT.get(key);
  if (existing && !forceFresh) return existing;
  const promise = fetchCalendarEvents(
    instantForWallDate(start, timeZone).toISOString(),
    instantForWallDate(end, timeZone).toISOString(),
    signal,
  ).then((rows) => {
    // A forced refresh can replace an older request for the same range. Do
    // not let that older response land later and put stale chips back in cache.
    if (CALENDAR_RANGE_INFLIGHT.get(key) === promise) rememberCalendarRange(key, rows);
    return rows;
  }).finally(() => {
    if (CALENDAR_RANGE_INFLIGHT.get(key) === promise) CALENDAR_RANGE_INFLIGHT.delete(key);
  });
  CALENDAR_RANGE_INFLIGHT.set(key, promise);
  return promise;
}

/** Load the visible range before the calendar page opens, so the grid can paint from cache. */
export function warmCalendarCache(): void {
  const stored = localStorage.getItem("calendar-view");
  const view: ViewMode = stored === "month" || stored === "day" || stored === "week" ? stored : "week";
  const timeZone = loadCalendarTimeZone();
  const anchor = wallDateForInstant(new Date(), timeZone);
  const start = rangeStart(anchor, view);
  const end = view === "month" ? addDays(start, 42) : endExclusive(start, view);
  if (CALENDAR_RANGE_CACHE.has(calendarRangeKey(start, end, timeZone))) return;
  void fetchCalendarRange(start, end, timeZone).catch(() => {});
  void fetchPendingInterviews().catch(() => {});
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
const dateTimeFormatters = new Map<string, Intl.DateTimeFormat>();
function cachedDateTime(timeZone: string, options: Intl.DateTimeFormatOptions, value: Date) {
  const key = `${timeZone}|${options.weekday ?? ""}|${options.month ?? ""}|${options.day ?? ""}|${options.year ?? ""}|${options.hour ?? ""}|${options.minute ?? ""}|${options.timeZoneName ?? ""}`;
  let formatter = dateTimeFormatters.get(key);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat("en-US", { timeZone, ...options });
    dateTimeFormatters.set(key, formatter);
  }
  return formatter.format(value);
}
function formatDateTime(value: string, timeZone: string) {
  return cachedDateTime(timeZone, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" }, new Date(value));
}

const INTERVIEW_CSV_HEADERS = ["profileEmail", "companyName", "jobTitle", "interviewDate", "interviewType"] as const;

function csvCell(value: string) {
  return /[",\r\n]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value;
}

function interviewDateForCsv(value: string, timeZone: string) {
  const parts = partsInTimeZone(new Date(value), timeZone);
  const pad = (number: number) => String(number).padStart(2, "0");
  return `${parts.year}-${pad(parts.month)}-${pad(parts.day)}`;
}

function interviewCsv(events: CalendarEvent[], timeZone: string) {
  const rows = [...events]
    .sort((a, b) => new Date(a.start_at).getTime() - new Date(b.start_at).getTime() || a.id - b.id)
    .map((event) => [
      event.email_account,
      event.company,
      event.role,
      interviewDateForCsv(event.start_at, timeZone),
      "Intro",
    ].map(csvCell).join(","));
  return [INTERVIEW_CSV_HEADERS.join(","), ...rows].join("\r\n");
}
function formatClock(value: string, timeZone: string) {
  return cachedDateTime(timeZone, { hour: "numeric", minute: "2-digit" }, new Date(value));
}
function formatInterviewTimeRange(event: CalendarEvent, timeZone: string) {
  const startInstant = new Date(event.start_at);
  const endInstant = new Date(event.end_at);
  const startParts = partsInTimeZone(startInstant, timeZone);
  const endParts = partsInTimeZone(endInstant, timeZone);
  const sameDay = startParts.year === endParts.year && startParts.month === endParts.month && startParts.day === endParts.day;
  const dateOptions: Intl.DateTimeFormatOptions = { weekday: "short", month: "short", day: "numeric" };
  const startDate = cachedDateTime(timeZone, dateOptions, startInstant);
  const startClock = formatClock(event.start_at, timeZone);
  const endClock = formatClock(event.end_at, timeZone);
  if (sameDay) return `${startDate}, ${startClock} – ${endClock}`;
  const endDate = cachedDateTime(timeZone, dateOptions, endInstant);
  return `${startDate}, ${startClock} – ${endDate}, ${endClock}`;
}
function InterviewTimeRange({ event, timeZone }: { event: CalendarEvent; timeZone: string }) {
  const startInstant = new Date(event.start_at);
  const endInstant = new Date(event.end_at);
  const startParts = partsInTimeZone(startInstant, timeZone);
  const endParts = partsInTimeZone(endInstant, timeZone);
  const sameDay = startParts.year === endParts.year && startParts.month === endParts.month && startParts.day === endParts.day;
  const dateOptions: Intl.DateTimeFormatOptions = { weekday: "short", month: "short", day: "numeric" };
  const startDate = cachedDateTime(timeZone, dateOptions, startInstant);
  const startClock = formatClock(event.start_at, timeZone);
  const endClock = formatClock(event.end_at, timeZone);
  if (sameDay) {
    return (
      <span className="interview-time-range">
        <span>{startDate}</span>
        <span>{startClock} – {endClock}</span>
      </span>
    );
  }
  return (
    <span className="interview-time-range">
      <span>{startDate}, {startClock} –</span>
      <span>{cachedDateTime(timeZone, dateOptions, endInstant)}, {endClock}</span>
    </span>
  );
}
function periodTitle(anchor: Date, days: Date[], view: ViewMode) {
  const monthName = (date: Date, length: "long" | "short") => new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: length }).format(date);
  const weekdayName = (date: Date) => new Intl.DateTimeFormat("en-US", { timeZone: "UTC", weekday: "long" }).format(date);
  if (view === "month") return { primary: monthName(anchor, "long"), secondary: String(anchor.getUTCFullYear()) };
  if (view === "day") {
    const day = days[0] ?? anchor;
    return { primary: `${weekdayName(day)}, ${monthName(day, "long")} ${day.getUTCDate()}`, secondary: String(day.getUTCFullYear()) };
  }
  const first = days[0];
  const last = days[days.length - 1];
  const startLabel = `${monthName(first, "short")} ${first.getUTCDate()}`;
  const endLabel = `${monthName(last, "short")} ${last.getUTCDate()}`;
  if (first.getUTCFullYear() === last.getUTCFullYear()) {
    return { primary: `${startLabel} – ${endLabel}`, secondary: String(last.getUTCFullYear()) };
  }
  return { primary: `${startLabel} ${first.getUTCFullYear()} – ${endLabel} ${last.getUTCFullYear()}`, secondary: "" };
}
function sameDay(a: Date, b: Date) { return startOfDay(a).getTime() === startOfDay(b).getTime(); }
function isAllDay(event: CalendarEvent, timeZone: string) {
  const instantStart = new Date(event.start_at); const wallStart = wallDateForInstant(instantStart, timeZone); const end = new Date(event.end_at);
  return wallStart.getUTCHours() === 0 && wallStart.getUTCMinutes() === 0 && end.getTime() - instantStart.getTime() >= 23 * 60 * 60 * 1000;
}
type DayEventGroup = { all: CalendarEvent[]; allDay: CalendarEvent[]; timed: CalendarEvent[] };
function groupEventsByDay(events: CalendarEvent[], days: Date[], timeZone: string): DayEventGroup[] {
  const bounds = days.map((day) => ({
    start: instantForWallDate(startOfDay(day), timeZone).getTime(),
    end: instantForWallDate(addDays(startOfDay(day), 1), timeZone).getTime(),
  }));
  const groups: DayEventGroup[] = bounds.map(() => ({ all: [], allDay: [], timed: [] }));
  const ordered = events.map((event) => ({
    event,
    start: Date.parse(event.start_at),
    end: Date.parse(event.end_at),
    allDay: isAllDay(event, timeZone),
  })).sort((a, b) => a.start - b.start || a.event.id - b.event.id);
  for (const item of ordered) {
    for (let index = 0; index < bounds.length; index += 1) {
      const bound = bounds[index];
      if (item.start >= bound.end || item.end <= bound.start) continue;
      const group = groups[index];
      group.all.push(item.event);
      (item.allDay ? group.allDay : group.timed).push(item.event);
    }
  }
  return groups;
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

type VisibleApplicationStatus = Exclude<CalendarEvent["application_status"], "active"> | "canceled";

function visibleApplicationStatus(event: CalendarEvent): VisibleApplicationStatus | null {
  if (event.interview_status === "cancellation") return "canceled";
  return event.application_status === "active" ? null : event.application_status;
}

function eventStatusFilter(event: CalendarEvent): CalendarStatusFilter {
  if (event.kind !== "confirmed") return "available";
  const outcome = visibleApplicationStatus(event);
  if (outcome == null) return "active";
  if (outcome === "rejected" || outcome === "position_closed") return "rejected_closed";
  return outcome;
}

function defaultStatusFilters(): CalendarStatusFilter[] {
  return CALENDAR_STATUS_FILTERS.map((item) => item.value).filter((value) => value !== "canceled");
}

const CALENDAR_HIDDEN_MAILBOXES_KEY = "calendar-hidden-mailboxes";

function loadHiddenMailboxIds(): number[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(CALENDAR_HIDDEN_MAILBOXES_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter((value): value is number => Number.isInteger(value)) : [];
  } catch {
    return [];
  }
}

function persistHiddenMailboxIds(next: number[]) {
  try { localStorage.setItem(CALENDAR_HIDDEN_MAILBOXES_KEY, JSON.stringify(next)); }
  catch { /* ignore unavailable storage */ }
}

function loadStatusFilters(): CalendarStatusFilter[] {
  const defaults = defaultStatusFilters();
  try {
    const stored = localStorage.getItem(CALENDAR_STATUS_VIEW_KEY);
    if (!stored || stored === "all") return defaults;
    const parsed: unknown = stored.startsWith("[") ? JSON.parse(stored) : [stored];
    if (!Array.isArray(parsed)) return defaults;
    const migrated = parsed.flatMap((value): CalendarStatusFilter[] => {
      if (value === "rejected" || value === "closed" || value === "rejected_closed") return ["rejected_closed"];
      if (value === "active" || value === "passed" || value === "canceled" || value === "available") return [value];
      return [];
    });
    const unique = [...new Set(migrated)];
    return unique.length ? unique : defaults;
  } catch {
    return defaults;
  }
}

function persistStatusFilters(next: CalendarStatusFilter[]) {
  try { localStorage.setItem(CALENDAR_STATUS_VIEW_KEY, JSON.stringify(next)); }
  catch { /* ignore unavailable storage */ }
}

type CardStatus = { key: "active" | "passed" | "rejected_closed" | "canceled" | "available"; label: string; className: string };
type ConfirmableStatus = Exclude<CardStatus["key"], "available">;

function cardStatus(event: CalendarEvent): CardStatus {
  if (event.kind !== "confirmed") return { key: "available", label: "Available", className: "" };
  if (event.interview_status === "cancellation") return { key: "canceled", label: "Canceled", className: "canceled status-canceled" };
  if (event.application_status === "passed") return { key: "passed", label: "Passed", className: "status-passed" };
  if (event.application_status === "rejected" || event.application_status === "position_closed") return { key: "rejected_closed", label: "Rejected/Closed", className: "status-rejected" };
  // "active" is the persisted application state. On the calendar, however,
  // this row is a confirmed appointment, so calling it Active makes it look
  // like another open availability slot. Use the booking state in UI copy.
  return { key: "active", label: "Booked", className: "status-active" };
}

function ActiveStatusIcon() {
  return <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3.2 8.3 3.1 3.1L12.8 4.6" /></svg>;
}

function ApplicationStatusIcon({ status }: { status: CalendarEvent["application_status"] }) {
  if (status === "passed") return <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 12h6M6 12.2a2 2 0 0 0 4 0M4.5 10.5h7l-1-1.5V6a2.5 2.5 0 0 0-5 0v3l-1 1.5Z" /></svg>;
  if (status === "position_closed") return <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 5.5h9v7h-9zM5.5 5.5V4a2.5 2.5 0 0 1 5 0v1.5M6 9h4" /></svg>;
  if (status === "rejected") return <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 4 8 8M12 4l-8 8" /></svg>;
  return <ActiveStatusIcon />;
}

function CanceledStatusIcon() {
  return <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.5" /><path d="m4.1 4.1 7.8 7.8" /></svg>;
}

function pendingBadgeLabel(event: CalendarEvent) {
  return event.request_kind === "reschedule" ? "Rescheduled time offered, not booked yet" : "Available time offered, not booked yet";
}

function PendingRequestIcon({ kind }: { kind: "invitation" | "reschedule" }) {
  if (kind === "reschedule") return <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M12.6 6.2A4.9 4.9 0 0 0 3.6 5" /><path d="M3.4 2.6V5.2H6" /><path d="M3.4 9.8a4.9 4.9 0 0 0 9 1.2" /><path d="M12.6 13.4v-2.6H10" /></svg>;
  return <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 2.5h7M4.5 13.5h7" /><path d="M5.2 2.5c0 2.6 2.8 3.6 2.8 5.5S5.2 10.9 5.2 13.5M10.8 2.5c0 2.6-2.8 3.6-2.8 5.5s2.8 2.9 2.8 5.5" /></svg>;
}

function VisibleApplicationStatusIcon({ status }: { status: VisibleApplicationStatus }) {
  return status === "canceled" ? <CanceledStatusIcon /> : <ApplicationStatusIcon status={status} />;
}

function messageCancelsAppointment(message: EmailDetail): boolean {
  if (message.interview_subtype === "cancellation") return true;
  if (/\bcancel(?:ed|led|lation)\b/i.test(message.subject || "")) return true;
  const calendar = `${message.subject || ""}\n${message.body_text || ""}`.match(/BEGIN:VCALENDAR[\s\S]*?END:VCALENDAR/i);
  if (!calendar) return false;
  return /^METHOD:CANCEL\b/im.test(calendar[0]) || /^STATUS:CANCELLED\b/im.test(calendar[0]);
}

function emailInterviewStatus(message: EmailDetail): { key: ConfirmableStatus; label: string; className: string; applicationStatus?: CalendarEvent["application_status"] } | null {
  if (messageCancelsAppointment(message)) return { key: "canceled", label: "Canceled", className: "status-canceled" };
  const text = `${message.subject || ""} ${message.snippet || ""} ${message.body_text || ""}`;
  if (message.label === "rejected_closed") {
    const closed = ["position has been closed", "role has been closed", "position is no longer available", "job has been filled"].some((pattern) => text.toLowerCase().includes(pattern));
    return { key: "rejected_closed", label: "Rejected/Closed", className: "status-rejected", applicationStatus: closed ? "position_closed" : "rejected" };
  }
  if (message.label === "offer") return { key: "passed", label: "Passed", className: "status-passed" };
  if (message.label === "interview_follow_up") {
    const passed = /\byou (?:have )?passed\b|\bsuccessfully (?:passed|cleared)\b|\b(?:move|moving|advance|advancing) (?:you )?(?:forward|to the next)\b|\b(?:invite|inviting) you to (?:the )?next (?:round|stage)\b|\b(?:pleased|happy|excited) to (?:move|invite|advance) you\b|\bcongratulations\b.{0,120}\bnext (?:round|stage)\b/i.test(text);
    return passed ? { key: "passed", label: "Passed", className: "status-passed" } : null;
  }
  if (message.label === "interview_scheduled") return { key: "active", label: "Booked", className: "status-active" };
  return null;
}

function interviewType(event: CalendarEvent): { type: CalendarEvent["meeting_type"]; label: string } {
  // The explicit type is authoritative. Supporting fields are fallbacks for
  // legacy rows; a placeholder provider must never turn a phone call into a
  // video chip.
  if (event.meeting_type === "video") return { type: "video", label: "Video call" };
  if (event.meeting_type === "phone") return { type: "phone", label: "Phone call" };
  if (event.meeting_type === "in_person") return { type: "in_person", label: "In-person interview" };
  if (event.phone_number) return { type: "phone", label: "Phone call" };
  if (event.meeting_url || normalizedMeetingProvider(event.meeting_provider)) return { type: "video", label: "Video call" };
  return { type: "unspecified", label: "Not specified" };
}

const MEETING_PROVIDER_PLACEHOLDERS = new Set(["empty", "n/a", "na", "none", "not available", "not provided", "not specified", "null", "tba", "tbc", "tbd", "unknown", "unspecified", "-"]);

function normalizedMeetingProvider(value: string | null | undefined): string | null {
  const provider = value?.trim() ?? "";
  return provider && !MEETING_PROVIDER_PLACEHOLDERS.has(provider.toLowerCase()) ? provider : null;
}

function meetingProviderLabel(event: CalendarEvent): string {
  const provider = normalizedMeetingProvider(event.meeting_provider);
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

let activeEventHoverId: string | null = null;
const eventHoverListeners = new Set<(id: string | null) => void>();
function setActiveEventHover(id: string | null) {
  if (activeEventHoverId === id) return;
  activeEventHoverId = id;
  eventHoverListeners.forEach((listener) => listener(id));
}

const EventCard = memo(function EventCard({ event, color, timeZone, compact, timed, position, selected, onSelect }: { event: CalendarEvent; color: string; timeZone: string; compact?: boolean; timed?: boolean; position?: React.CSSProperties; selected?: boolean; onSelect: (event: CalendarEvent) => void }) {
  const eventRef = useRef<HTMLDivElement>(null);
  const hoverId = useId();
  const [hoverPosition, setHoverPosition] = useState<{ left: number; top: number; above: boolean } | null>(null);
  const [hoverVisible, setHoverVisible] = useState(false);
  const hoverHideTimer = useRef<number | null>(null);
  const hoverFrame = useRef<number | null>(null);
  const showingHover = useRef(false);
  const status = cardStatus(event);
  const outcome = visibleApplicationStatus(event);
  const outcomeLabel = status.key === "available" ? null : status.label;
  const type = interviewType(event);
  const primaryLabel = event.company || event.title || event.role || "Interview";
  const showRole = status.key === "available" && Boolean(event.role) && event.role.trim().toLocaleLowerCase() !== primaryLabel.trim().toLocaleLowerCase();
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
    showingHover.current = true;
    setActiveEventHover(hoverId);
    setHoverPosition(nextPosition);
    setHoverVisible(false);
    hoverFrame.current = window.requestAnimationFrame(() => {
      setHoverVisible(true);
      hoverFrame.current = null;
    });
  }, [getHoverPosition, hoverId]);
  const hideHover = useCallback(() => {
    showingHover.current = false;
    if (hoverFrame.current != null) window.cancelAnimationFrame(hoverFrame.current);
    if (hoverHideTimer.current != null) window.clearTimeout(hoverHideTimer.current);
    setHoverVisible(false);
    hoverHideTimer.current = window.setTimeout(() => {
      setHoverPosition(null);
      hoverHideTimer.current = null;
    }, calendarMotionDelay(150));
  }, []);
  const hideHoverRef = useRef(hideHover);
  hideHoverRef.current = hideHover;
  useEffect(() => {
    const listener = (next: string | null) => {
      if (next !== hoverId && showingHover.current) hideHoverRef.current();
    };
    eventHoverListeners.add(listener);
    return () => {
      eventHoverListeners.delete(listener);
      if (activeEventHoverId === hoverId) setActiveEventHover(null);
    };
  }, [hoverId]);
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
  const selectEvent = () => { hideHover(); setActiveEventHover(null); onSelect(event); };
  return <>
    <div
    ref={eventRef}
    className={`calendar-event ${event.kind} ${status.className} ${compact ? "compact" : ""} ${timed ? "timed" : ""} ${selected ? "selected" : ""} has-meeting-type ${status.key !== "active" ? "has-status" : ""} ${timed && durationMinutes <= 30 ? "very-short" : timed && durationMinutes < 60 ? "short" : ""}`}
    style={{ "--account-color": color, "--event-ink": accountInk(color), ...position } as React.CSSProperties}
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
    aria-label={`${interviewKindLabel(event)}, ${primaryLabel}, ${formatInterviewTimeRange(event, timeZone)}, ${type.label}${outcomeLabel ? `, ${outcomeLabel}` : ""}${status.key === "available" ? `, ${pendingBadgeLabel(event)}` : ""}`}
    aria-describedby={hoverPosition ? hoverId : undefined}
  >
    {status.key === "available" && <span className={`calendar-event-status-icon calendar-event-pending-icon is-${event.request_kind ?? "invitation"}`} role="img" aria-label={pendingBadgeLabel(event)}><PendingRequestIcon kind={event.request_kind ?? "invitation"} /></span>}
    {status.key !== "available" && status.key !== "active" && <span className="calendar-event-status-icon" role="img" aria-label={status.label}>{status.key === "canceled" ? <CanceledStatusIcon /> : <ApplicationStatusIcon status={status.key === "rejected_closed" ? "rejected" : status.key} />}</span>}
    <span className="calendar-event-main">
      <span className="calendar-event-company">{primaryLabel}</span>
      {showRole && <span className="calendar-event-role">{event.role}</span>}
    </span>
    <span className={`event-type-badge meeting-${type.type}`} role="img" aria-label={type.label}><MeetingTypeIcon type={type.type} /></span>
  </div>
    {hoverPosition && createPortal(
      <div
        id={hoverId}
        className={`calendar-event-hover-card ${hoverPosition.above ? "above" : "below"} ${hoverVisible ? "is-visible" : ""}`}
        role="tooltip"
        style={{ left: hoverPosition.left, top: hoverPosition.top }}
      >
        <dl>
          <div className="calendar-hover-time"><dt>Interview time</dt><dd><InterviewTimeRange event={event} timeZone={timeZone} /></dd></div>
          <div><dt>Account</dt><dd>{event.email_account || "—"}</dd></div>
          <div><dt>Company</dt><dd>{event.company || "—"}</dd></div>
          <div><dt>Role</dt><dd>{event.role || "—"}</dd></div>
          <div className={`calendar-hover-type meeting-${type.type}`}>
            <dt>Interview type</dt>
            <dd><MeetingTypeIcon type={type.type} /><span>{type.label}</span></dd>
          </div>
          {status.key === "available" && <div className={`calendar-hover-offer is-${event.request_kind ?? "invitation"}`}><dt>Status</dt><dd><PendingRequestIcon kind={event.request_kind ?? "invitation"} /><span>{event.request_kind === "reschedule" ? "Reschedule offered, not booked yet" : "Available time, not booked yet"}</span></dd></div>}
          {status.key !== "available" && status.key !== "active" && <div className={`calendar-hover-status ${status.className}`}><dt>Status</dt><dd>{outcome ? <VisibleApplicationStatusIcon status={outcome} /> : <ApplicationStatusIcon status="active" />}<span>{status.label}</span></dd></div>}
        </dl>
      </div>,
      document.body,
    )}
  </>;
});

function monthDayLabel(day: Date) {
  return new Intl.DateTimeFormat(undefined, { timeZone: "UTC", weekday: "long", month: "short", day: "numeric" }).format(day);
}

function HolidayChip({ holiday }: { holiday: UsHoliday }) {
  return <div className="calendar-holiday" title={`U.S. federal holiday: ${holiday.name}`} aria-label={`U.S. federal holiday, ${holiday.name}`}><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m8 1.6 1.85 3.75 4.14.6-3 2.92.71 4.12L8 11.04 4.3 13l.71-4.13-3-2.92 4.14-.6L8 1.6Z" /></svg><strong>{holiday.name}</strong></div>;
}

function MonthDayAgenda({
  day,
  events,
  holidays,
  timeZone,
  colorFor,
  onClose,
  onSelect,
  onCreate,
}: {
  day: Date;
  events: CalendarEvent[];
  holidays: UsHoliday[];
  timeZone: string;
  colorFor: (event: CalendarEvent) => string;
  onClose: () => void;
  onSelect: (event: CalendarEvent) => void;
  onCreate: (() => void) | null;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState<{ left: number; top?: number; bottom?: number; maxHeight: number; width: number } | null>(null);
  const place = useCallback(() => {
    const cell = document.querySelector<HTMLElement>(`[data-month-day="${day.toISOString()}"]`);
    if (!cell) return;
    const rect = cell.getBoundingClientRect();
    const width = Math.min(320, window.innerWidth - 24);
    const left = Math.max(12, Math.min(rect.left, window.innerWidth - width - 12));
    const gap = 8;
    const spaceBelow = window.innerHeight - rect.bottom - gap - 12;
    const spaceAbove = rect.top - gap - 12;
    const preferBelow = spaceBelow >= 200 || spaceBelow >= spaceAbove;
    const maxHeight = Math.max(168, Math.min(420, preferBelow ? spaceBelow : spaceAbove));
    // Below the cell, anchor the panel's top edge. Above the cell, anchor its
    // bottom edge so a short list sits next to the clicked day instead of
    // being offset by the maximum permitted height.
    const top = preferBelow ? rect.bottom + gap : undefined;
    const bottom = preferBelow ? undefined : window.innerHeight - rect.top + gap;
    setBox((current) => current && current.left === left && current.top === top && current.bottom === bottom && current.maxHeight === maxHeight && current.width === width ? current : { left, top, bottom, maxHeight, width });
  }, [day]);
  useLayoutEffect(() => {
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [place]);
  const focused = useRef(false);
  useEffect(() => {
    if (!box || focused.current) return;
    focused.current = true;
    panelRef.current?.focus();
  }, [box]);
  if (!box) return null;
  const ordered = [...events].sort((a, b) => new Date(a.start_at).getTime() - new Date(b.start_at).getTime() || a.id - b.id);
  return createPortal(
    <div
      ref={panelRef}
      className="month-day-agenda"
      data-month-agenda=""
      role="dialog"
      aria-label={`${monthDayLabel(day)} interviews`}
      tabIndex={-1}
      style={{ left: box.left, top: box.top, bottom: box.bottom, width: box.width, maxHeight: box.maxHeight }}
    >
      <header>
        <div>
          <strong>{monthDayLabel(day)}</strong>
          <span>{ordered.length === 0 && holidays.length === 0 ? "Nothing scheduled" : `${ordered.length + holidays.length} calendar item${ordered.length + holidays.length === 1 ? "" : "s"}`}</span>
        </div>
        <button type="button" onClick={onClose} aria-label="Close day">×</button>
      </header>
      {ordered.length === 0 && holidays.length === 0 ? <p className="month-day-agenda-empty">Nothing is scheduled for this day.</p> : <ul>
        {holidays.map((holiday) => <li className="month-day-agenda-holiday" key={holiday.id}><HolidayChip holiday={holiday} /></li>)}
        {ordered.map((event) => {
          const status = cardStatus(event);
          return <li key={event.id}>
            <button type="button" onClick={() => onSelect(event)}>
              <span className="month-day-agenda-swatch" style={{ background: colorFor(event) }} />
              <span className="month-day-agenda-copy">
                <strong>{event.company || event.title || "Interview"}</strong>
                <small>{formatClock(event.start_at, timeZone)} – {formatClock(event.end_at, timeZone)}{event.role ? ` · ${event.role}` : ""}</small>
              </span>
              {status.key !== "active" && status.key !== "available" && <em className={status.className}>{status.label}</em>}
            </button>
          </li>;
        })}
      </ul>}
      {onCreate && <footer><button type="button" onClick={onCreate}>Add event</button></footer>}
    </div>,
    document.body,
  );
}

type AccountInterviewCount = {
  mailboxId: number;
  email: string;
  count: number;
  color: string;
};

const DONUT_RADIUS = 42;
const DONUT_CIRCUMFERENCE = 2 * Math.PI * DONUT_RADIUS;

function requestedAgo(value: string | null) {
  if (!value) return "";
  const days = Math.floor((Date.now() - new Date(value).getTime()) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  return `${days} days ago`;
}

const PENDING_DISMISSED_KEY = "calendar.pendingAlert.dismissed";

function readDismissedPending(): number[] {
  try {
    localStorage.removeItem(PENDING_DISMISSED_KEY);
    const value = JSON.parse(sessionStorage.getItem(PENDING_DISMISSED_KEY) || "[]");
    return Array.isArray(value) ? value.filter((id) => typeof id === "number") : [];
  } catch {
    return [];
  }
}

function PendingInterviewCard({ item, isNew, timeZone, color, onJump }: {
  item: PendingInterview;
  isNew: boolean;
  timeZone: string;
  color: string;
  onJump: (startAt: string) => void;
}) {
  return (
    <li className={`pending-interview is-${item.kind}`} style={{ "--account-color": color } as React.CSSProperties}>
      <div className="pending-interview-top">
        <span className={`pending-interview-kind is-${item.kind}`}>
          <PendingKindIcon kind={item.kind} />
          {item.kind === "reschedule" ? "Reschedule" : "Invitation"}
        </span>
        {isNew && <span className="pending-interview-new">New</span>}
        {item.received_at && <span className="pending-interview-age">{requestedAgo(item.received_at)}</span>}
      </div>
      <strong className="pending-interview-company" title={item.company || item.subject}>{item.company || item.subject || "Interview request"}</strong>
      <span className="pending-interview-role" title={item.role || undefined}>{item.role || "Role not mentioned"}</span>
      <span className="pending-interview-meta">
        <span className="pending-interview-dot" aria-hidden="true" />
        <span className="pending-interview-account">{item.email_account}</span>
      </span>
      {item.slots.length > 0 ? (
        <div className="pending-interview-slots" aria-label="Proposed times">
          {item.slots.map((slot) => (
            <button type="button" key={slot.event_id} onClick={() => onJump(slot.start_at)} title="Show this day on the calendar">
              <span>{cachedDateTime(timeZone, { weekday: "short", month: "short", day: "numeric" }, new Date(slot.start_at))}</span>
              <small>
                {cachedDateTime(timeZone, { hour: "numeric", minute: "2-digit" }, new Date(slot.start_at))}
                {" – "}
                {cachedDateTime(timeZone, { hour: "numeric", minute: "2-digit" }, new Date(slot.end_at))}
              </small>
            </button>
          ))}
        </div>
      ) : <span className="pending-interview-note">No times proposed yet. Reply with your availability.</span>}
    </li>
  );
}

function pendingSummary(items: PendingInterview[]) {
  const invitations = items.filter((item) => item.kind !== "reschedule").length;
  const reschedules = items.length - invitations;
  const parts = [];
  if (invitations) parts.push(`${invitations} invitation${invitations === 1 ? "" : "s"}`);
  if (reschedules) parts.push(`${reschedules} reschedule${reschedules === 1 ? "" : "s"}`);
  return parts.join(" and ");
}

function PendingKindIcon({ kind }: { kind: PendingInterview["kind"] }) {
  return kind === "reschedule"
    ? <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M13 5.5A5.5 5.5 0 0 0 3.2 4.3M3 2v2.6h2.6M3 10.5a5.5 5.5 0 0 0 9.8 1.2M13 14v-2.6h-2.6" /></svg>
    : <svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="3.5" width="11" height="10" rx="2" /><path d="M2.5 6.5h11M5.5 2v3M10.5 2v3" /></svg>;
}

function PendingInterviewsAlert({ items, timeZone, mailboxColor, onJump }: {
  items: PendingInterview[];
  timeZone: string;
  mailboxColor: (mailboxId: number) => string;
  onJump: (startAt: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [dismissed, setDismissed] = useState<number[]>(readDismissedPending);
  const hasNew = items.some((item) => !dismissed.includes(item.email_id));
  if (items.length === 0 || !hasNew) return null;
  const newCount = items.filter((item) => !dismissed.includes(item.email_id)).length;
  const dismiss = () => {
    const ids = items.map((item) => item.email_id);
    try { sessionStorage.setItem(PENDING_DISMISSED_KEY, JSON.stringify(ids)); } catch { /* ignore unavailable storage */ }
    setDismissed(ids);
    setExpanded(false);
  };
  const companies = items.map((item) => item.company || item.subject || "Interview request");
  return (
    <section className={`pending-alert ${expanded ? "is-expanded" : ""}`} role="status" aria-live="polite">
      <div className="pending-alert-bar">
        <span className="pending-alert-icon" aria-hidden="true">
          <svg viewBox="0 0 20 20"><path d="M10 2.8a5 5 0 0 0-5 5v2.7l-1.4 2.6c-.3.5.1 1.1.7 1.1h11.4c.6 0 1-.6.7-1.1L15 10.5V7.8a5 5 0 0 0-5-5ZM8 16.2a2 2 0 0 0 4 0" /></svg>
          {newCount > 0 && <span className="pending-alert-ping" />}
        </span>
        <div className="pending-alert-text">
          <strong>{items.length} interview{items.length === 1 ? "" : "s"} waiting to be scheduled</strong>
          <span title={companies.join(", ")}>
            {pendingSummary(items)}
            {!expanded && <> · {companies.slice(0, 3).join(", ")}{companies.length > 3 ? ` +${companies.length - 3} more` : ""}</>}
          </span>
        </div>
        <button type="button" className="pending-alert-toggle" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
          {expanded ? "Hide" : "Review"}
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg>
        </button>
        <button type="button" className="pending-alert-close" aria-label="Hide for this session" data-tooltip="Hide for this session" data-tooltip-hint="Comes back when a new request arrives" data-tooltip-placement="bottom" onClick={dismiss}>
          <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4.5 4.5 7 7m0-7-7 7" /></svg>
        </button>
      </div>
      {expanded && (
        <ul className="pending-alert-list">
          {items.map((item) => <PendingInterviewCard key={item.email_id} item={item} isNew={!dismissed.includes(item.email_id)} timeZone={timeZone} color={mailboxColor(item.mailbox_id)} onJump={onJump} />)}
        </ul>
      )}
    </section>
  );
}

function ScheduledInterviewChart({ accounts, exportCount, onExport }: { accounts: AccountInterviewCount[]; exportCount: number; onExport: () => void }) {
  const [hoveredId, setHoveredId] = useState<number | null>(null);
  const total = accounts.reduce((sum, account) => sum + account.count, 0);
  const activeAccounts = accounts.filter((account) => account.count > 0);
  const sorted = [...accounts].sort((a, b) => b.count - a.count);
  const hovered = activeAccounts.find((account) => account.mailboxId === hoveredId) ?? null;
  const gap = activeAccounts.length > 1 ? 3 : 0;
  let offset = 0;
  const segments = activeAccounts.map((account) => {
    const length = (account.count / total) * DONUT_CIRCUMFERENCE;
    const segment = { account, dash: Math.max(length - gap, 0.5), offset };
    offset += length;
    return segment;
  });
  const ariaSummary = total === 0
    ? "No scheduled interviews in this view"
    : `${total} scheduled interview${total === 1 ? "" : "s"} in this view. ${activeAccounts.map((account) => `${account.email}: ${account.count}`).join(". ")}`;
  const percentOf = (count: number) => total > 0 ? Math.round((count / total) * 100) : 0;

  return <section className="calendar-interview-summary" aria-label="Scheduled interviews by email account">
    <div className="interview-chart-card">
      <header className="interview-chart-header">
        <h2>Scheduled interviews</h2>
        <div className="interview-chart-actions">
          <span>This view</span>
          <button type="button" disabled={exportCount === 0} onClick={onExport} title={exportCount ? `Export ${exportCount} interview${exportCount === 1 ? "" : "s"} to CSV` : "No email-created interviews to export in this view"}>
            <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2v8m-3-3 3 3 3-3M3 12.5v1h10v-1" /></svg>
            Export CSV
          </button>
        </div>
      </header>
      <div className="interview-donut" role="img" aria-label={ariaSummary}>
        <svg viewBox="0 0 100 100" aria-hidden="true">
          <circle className="interview-donut-track" cx="50" cy="50" r={DONUT_RADIUS} />
          {segments.map(({ account, dash, offset: start }) => (
            <circle
              key={account.mailboxId}
              className={`interview-donut-segment${hovered && hovered.mailboxId !== account.mailboxId ? " is-dimmed" : ""}${hovered?.mailboxId === account.mailboxId ? " is-active" : ""}`}
              cx="50" cy="50" r={DONUT_RADIUS}
              stroke={account.color}
              strokeDasharray={`${dash} ${DONUT_CIRCUMFERENCE}`}
              strokeDashoffset={-start}
              onMouseEnter={() => setHoveredId(account.mailboxId)}
              onMouseLeave={() => setHoveredId(null)}
            />
          ))}
        </svg>
        <div className="interview-donut-center">
          <strong>{hovered ? hovered.count : total}</strong>
          <span>{hovered ? `${percentOf(hovered.count)}% · ${hovered.email.split("@")[0]}` : total === 1 ? "interview" : "interviews"}</span>
        </div>
      </div>
      <ul className="interview-account-list" aria-label="Scheduled interview counts by email account">
        {sorted.map((account) => {
          const percentage = percentOf(account.count);
          const [name, domain] = account.email.split("@");
          return <li
            className={`${account.count === 0 ? "is-empty" : ""}${hovered?.mailboxId === account.mailboxId ? " is-active" : ""}`}
            key={account.mailboxId}
            style={{ "--account-color": account.color } as React.CSSProperties}
            onMouseEnter={() => account.count > 0 && setHoveredId(account.mailboxId)}
            onMouseLeave={() => setHoveredId(null)}
          >
            <span className="interview-account-swatch" aria-hidden="true" />
            <span className="interview-account-copy">
              <strong>{name}<em>@{domain}</em></strong>
              <span className="interview-account-bar" aria-hidden="true"><span style={{ width: `${percentage}%` }} /></span>
            </span>
            <span className="interview-account-stat" aria-label={`${account.count} scheduled, ${percentage}%`}>
              <strong>{account.count}</strong>
              <small>{percentage}%</small>
            </span>
          </li>;
        })}
      </ul>
      {!accounts.length && <p className="interview-chart-empty">Select a calendar to see interview activity.</p>}
    </div>
  </section>;
}

type Editor = { id?: number; mailbox_id: number; title: string; company: string; role: string; interviewer: string; work_model: CalendarEvent["work_model"]; application_status: CalendarEvent["application_status"]; interview_status: CalendarEvent["interview_status"]; start: string; end: string; description: string; job_url: string; meeting_type: CalendarEvent["meeting_type"]; meeting_provider: string; meeting_url: string; phone_number: string; phone_access_code: string };
function editorFor(date: Date, mailboxId: number, timeZone: string, event?: CalendarEvent, selectedEnd?: Date): Editor {
  const start = event ? wallDateForInstant(new Date(event.start_at), timeZone) : selectedEnd ? new Date(date) : new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate(), 9));
  const end = event ? wallDateForInstant(new Date(event.end_at), timeZone) : selectedEnd ?? new Date(start.getTime() + DEFAULT_EVENT_DURATION_MINUTES * 60_000);
  return { id: event?.id, mailbox_id: event?.mailbox_id ?? mailboxId, title: event?.title ?? "Interview", company: event?.company ?? "", role: event?.role ?? "", interviewer: event?.interviewer ?? "", work_model: event?.work_model ?? "unspecified", application_status: event?.application_status ?? "active", interview_status: event?.interview_status ?? "confirmation", start: localInput(start), end: localInput(end), description: event?.description ?? "", job_url: event?.job_url ?? "", meeting_type: event?.meeting_type ?? "unspecified", meeting_provider: normalizedMeetingProvider(event?.meeting_provider) ?? "", meeting_url: event?.meeting_url ?? "", phone_number: event?.phone_number ?? "", phone_access_code: event?.phone_access_code ?? "" };
}

function CalendarLoading({ soft = false }: { soft?: boolean }) {
  return (
    <div className={`calendar-loading${soft ? " is-soft" : ""}`} role="status" aria-live="polite">
      <div className="calendar-loading-card">
        <span className="calendar-loading-bar" aria-hidden="true"><span /></span>
        <span className="calendar-loading-copy">{soft ? "Updating…" : "Loading calendar"}</span>
      </div>
    </div>
  );
}

const MONTH_CHIP_GAP = 6;

const MonthDayCell = memo(function MonthDayCell({
  day,
  events,
  holidays,
  today,
  outside,
  selected,
  selectedEventId,
  timeZone,
  colorFor,
  onSelectEvent,
  onToggle,
  onOpen,
}: {
  day: Date;
  events: CalendarEvent[];
  holidays: UsHoliday[];
  today: boolean;
  outside: boolean;
  selected: boolean;
  selectedEventId: number | null;
  timeZone: string;
  colorFor: (mailboxId: number) => string;
  onSelectEvent: (event: CalendarEvent) => void;
  onToggle: (day: Date) => void;
  onOpen: (day: Date) => void;
}) {
  const eventsRef = useRef<HTMLDivElement>(null);
  const itemCount = events.length + holidays.length;
  const [visibleCount, setVisibleCount] = useState(() => Math.min(itemCount, 3));
  useLayoutEffect(() => {
    const area = eventsRef.current;
    if (!area) return;
    const update = () => {
      const sample = area.querySelector<HTMLElement>(":scope > .calendar-event, :scope > .calendar-holiday");
      const row = (sample?.offsetHeight ?? 26) + MONTH_CHIP_GAP;
      const next = Math.max(1, Math.floor((area.clientHeight + MONTH_CHIP_GAP) / row));
      setVisibleCount((current) => current === next ? current : next);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(area);
    return () => observer.disconnect();
  }, [itemCount]);
  const items = [...holidays.map((holiday) => ({ kind: "holiday" as const, holiday })), ...events.map((event) => ({ kind: "event" as const, event }))];
  const hidden = Math.max(0, items.length - visibleCount);
  const shown = hidden > 0 ? items.slice(0, visibleCount) : items;
  return <div className={`calendar-day ${today ? "today" : ""} ${outside ? "outside" : ""} ${selected ? "is-selected" : ""}`} data-month-day={day.toISOString()} onClick={() => onToggle(day)}>
    <div className="calendar-day-heading"><strong>{day.getUTCDate()}</strong>{today && <em>Today</em>}</div>
    <div className="calendar-day-events" ref={eventsRef}>{shown.map((item) => item.kind === "holiday" ? <HolidayChip holiday={item.holiday} key={item.holiday.id} /> : <EventCard event={item.event} color={colorFor(item.event.mailbox_id)} timeZone={timeZone} compact selected={selectedEventId === item.event.id} onSelect={onSelectEvent} key={item.event.id} />)}</div>
    {hidden > 0 && <button className="calendar-day-more" type="button" onClick={(event) => { event.stopPropagation(); onOpen(day); }}>+{hidden} more</button>}
  </div>;
});

type DragSelection = { dayIndex: number; startMinutes: number; endMinutes: number };

export default function CalendarPage({
  mailboxes,
  refreshKey,
  active = true,
  onClose,
}: {
  mailboxes: Mailbox[];
  refreshKey: number;
  active?: boolean;
  onClose: () => void;
}) {
  const [view, setView] = useState<ViewMode>(() => (localStorage.getItem("calendar-view") as ViewMode) || "week");
  const [timeZone, setTimeZone] = useState(loadCalendarTimeZone);
  const [anchor, setAnchor] = useState(() => wallDateForInstant(new Date(), timeZone));
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [statusFilters, setStatusFilters] = useState<CalendarStatusFilter[]>(loadStatusFilters);
  const [hiddenMailboxIds, setHiddenMailboxIds] = useState<number[]>(loadHiddenMailboxIds);
  const updateHiddenMailboxIds = useCallback((update: (current: number[]) => number[]) => {
    setHiddenMailboxIds((current) => {
      const next = update(current);
      persistHiddenMailboxIds(next);
      return next;
    });
  }, []);
  const [loading, setLoading] = useState(true); const [refreshing, setRefreshing] = useState(false); const [error, setError] = useState("");
  const [selected, setSelected] = useState<CalendarEvent | null>(null);
  const [monthDay, setMonthDay] = useState<Date | null>(null);
  const [detailsClosing, setDetailsClosing] = useState(false);
  const [copiedMeetingEventId, setCopiedMeetingEventId] = useState<number | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarClosing, setSidebarClosing] = useState(false);
  const [emailMessages, setEmailMessages] = useState<EmailDetail[]>([]); const [emailLoading, setEmailLoading] = useState(false); const [emailError, setEmailError] = useState(""); const [expandedEmailId, setExpandedEmailId] = useState<number | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null); const [saving, setSaving] = useState(false); const [saveError, setSaveError] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [statusSaving, setStatusSaving] = useState(false); const [statusError, setStatusError] = useState("");
  const [dragSelection, setDragSelection] = useState<DragSelection | null>(null);
  const [hourHeight, setHourHeight] = useState(HOUR_HEIGHT);
  const [panelWidth, setPanelWidth] = useState(loadCalendarPanelWidth);
  const [panelResizing, setPanelResizing] = useState(false);
  const refreshRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const detailsCloseTimer = useRef<number | null>(null);
  const sidebarCloseTimer = useRef<number | null>(null);
  const timelineRef = useRef<HTMLElement | null>(null);
  const timelineHeaderRef = useRef<HTMLDivElement | null>(null);
  const calendarMainRef = useRef<HTMLDivElement | null>(null);
  const panelResizeRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const panelWidthRef = useRef(panelWidth);
  panelWidthRef.current = panelWidth;
  const dragRef = useRef<{ dayIndex: number; anchorMinutes: number } | null>(null);
  const start = useMemo(() => rangeStart(anchor, view), [anchor, view]);
  const end = useMemo(() => view === "month" ? addDays(start, 42) : endExclusive(start, view), [start, view]);
  const rangeKey = `${start.getTime()}|${end.getTime()}|${timeZone}`;

  const prefetchRange = useCallback((rangeStartDate: Date, rangeEndDate: Date, zone: string) => {
    const key = calendarRangeKey(rangeStartDate, rangeEndDate, zone);
    if (CALENDAR_RANGE_CACHE.has(key)) return;
    void fetchCalendarRange(rangeStartDate, rangeEndDate, zone).catch(() => {});
  }, []);

  const load = useCallback(async (mode: "foreground" | "background" = "foreground", forceFresh = false) => {
    const request = ++refreshRef.current;
    const hasCached = CALENDAR_RANGE_CACHE.has(rangeKey);
    if (mode === "foreground") {
      setError("");
      if (!hasCached) setLoading(true);
      else setRefreshing(true);
    } else {
      setRefreshing(true);
    }
    const joined = forceFresh ? undefined : CALENDAR_RANGE_INFLIGHT.get(rangeKey);
    if (!joined) {
      abortRef.current?.abort();
      abortRef.current = new AbortController();
    }
    try {
      const rows = await (joined ?? fetchCalendarRange(start, end, timeZone, abortRef.current?.signal, forceFresh));
      if (request === refreshRef.current) {
        setEvents(rows);
        setSelected((current) => current ? rows.find((event) => event.id === current.id) ?? null : null);
        setError("");
        const spanDays = Math.max(1, Math.round((end.getTime() - start.getTime()) / 86_400_000));
        void prefetchRange(addDays(start, -spanDays), start, timeZone);
        void prefetchRange(end, addDays(end, spanDays), timeZone);
      }
    }
    catch (err) { if (request === refreshRef.current && (err as Error).name !== "AbortError") setError(err instanceof Error ? err.message : "Failed to load calendar"); }
    finally {
      if (request === refreshRef.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, [start, end, timeZone, rangeKey, prefetchRange]);
  const seenRefreshKey = useRef(refreshKey);
  useEffect(() => {
    const cached = CALENDAR_RANGE_CACHE.get(rangeKey);
    if (cached) {
      setEvents(cached);
      setLoading(false);
    }
    void load(cached ? "background" : "foreground");
    return () => abortRef.current?.abort();
  }, [load, rangeKey]);
  useEffect(() => {
    if (seenRefreshKey.current === refreshKey) return;
    seenRefreshKey.current = refreshKey;
    CALENDAR_RANGE_CACHE.clear();
    void load("background", true);
  }, [load, refreshKey]);
  const skipReopenLoad = useRef(true);
  useEffect(() => {
    if (!active) return;
    if (skipReopenLoad.current) {
      skipReopenLoad.current = false;
      return;
    }
    const cached = CALENDAR_RANGE_CACHE.get(rangeKey);
    if (cached) setEvents(cached);
    void load(cached ? "background" : "foreground");
  }, [active]); // eslint-disable-line react-hooks/exhaustive-deps -- reopen should paint cache then refresh quietly
  useEffect(() => {
    const source = openEvents();
    let timer = 0;
    let hasOpened = false;
    const onChange = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        CALENDAR_RANGE_CACHE.clear();
        void load("background", true);
      }, 400);
    };
    const onOpen = () => {
      // Re-fetch after an SSE reconnect because repairs may have completed
      // while the backend was restarting and no change event could be sent.
      if (hasOpened) onChange();
      hasOpened = true;
    };
    source.addEventListener("connected", onOpen);
    source.addEventListener("calendar.changed", onChange);
    return () => {
      window.clearTimeout(timer);
      source.close();
    };
  }, [load]);
  const activeMailboxKey = useMemo(
    () => mailboxes.filter((mailbox) => mailbox.is_active).map((mailbox) => mailbox.id).sort((a, b) => a - b).join(","),
    [mailboxes],
  );
  const seenMailboxKey = useRef(activeMailboxKey);
  useEffect(() => {
    if (seenMailboxKey.current === activeMailboxKey) return;
    seenMailboxKey.current = activeMailboxKey;
    CALENDAR_RANGE_CACHE.clear();
    void load("background", true);
  }, [activeMailboxKey, load]);
  const [pendingInterviews, setPendingInterviews] = useState<PendingInterview[]>([]);
  const loadPending = useCallback(async (signal?: AbortSignal) => {
    const cached = peekPendingInterviews();
    if (cached) setPendingInterviews(cached);
    try { setPendingInterviews(await fetchPendingInterviews(signal)); }
    catch (err) { if ((err as Error).name !== "AbortError") setPendingInterviews([]); }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void loadPending(controller.signal);
    return () => controller.abort();
  }, [loadPending, refreshKey]);
  useEffect(() => {
    const source = openEvents();
    let timer = 0;
    const onChange = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => { void loadPending(); }, 600);
    };
    source.addEventListener("calendar.changed", onChange);
    return () => {
      window.clearTimeout(timer);
      source.close();
    };
  }, [loadPending]);
  useEffect(() => { localStorage.setItem("calendar-view", view); }, [view]);
  useEffect(() => { try { localStorage.setItem(CALENDAR_TIME_ZONE_KEY, timeZone); } catch { /* ignore unavailable storage */ } }, [timeZone]);
  useEffect(() => () => {
    if (detailsCloseTimer.current != null) window.clearTimeout(detailsCloseTimer.current);
    if (sidebarCloseTimer.current != null) window.clearTimeout(sidebarCloseTimer.current);
  }, []);
  useLayoutEffect(() => {
    if (!active || view === "month") return;
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
  }, [view, active]);
  useEffect(() => {
    if (view !== "month" && timelineRef.current) timelineRef.current.scrollTop = 7 * hourHeight;
    setDragSelection(null); dragRef.current = null;
  }, [view, start.getTime(), hourHeight]);
  useEffect(() => {
    setEmailMessages([]); setEmailError(""); setExpandedEmailId(null); setStatusError("");
    if (!selected || selected.origin === "manual") {
      setEmailLoading(false);
      return;
    }
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
  useEffect(() => {
    if (view !== "month") {
      setMonthDay(null);
      return;
    }
    setMonthDay((current) => current && days.some((day) => sameDay(day, current)) ? current : null);
  }, [view, days]);
  useEffect(() => {
    if (!monthDay) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") setMonthDay(null); };
    const onPointer = (event: PointerEvent) => {
      const element = event.target instanceof Element ? event.target : null;
      if (element?.closest("[data-month-agenda], [data-month-day]")) return;
      setMonthDay(null);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [monthDay]);
  const miniMonthStart = useMemo(() => startOfWeek(new Date(Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth(), 1))), [anchor.getUTCFullYear(), anchor.getUTCMonth()]);
  const miniMonthDays = useMemo(() => Array.from({ length: 42 }, (_, i) => addDays(miniMonthStart, i)), [miniMonthStart.getTime()]);
  const shownEvents = useMemo(() => events.filter((event) => statusFilters.includes(eventStatusFilter(event)) && !hiddenMailboxIds.includes(event.mailbox_id)), [events, statusFilters, hiddenMailboxIds]);
  const activeMailboxes = useMemo(() => mailboxes.filter((mailbox) => mailbox.is_active), [mailboxes]);
  const allCalendarsShown = activeMailboxes.every((mailbox) => !hiddenMailboxIds.includes(mailbox.id));
  const dayGroups = useMemo(() => groupEventsByDay(shownEvents, days, timeZone), [shownEvents, days, timeZone]);
  const holidays = useMemo(() => usFederalHolidays(start, end), [start, end]);
  const holidaysByDay = useMemo(() => days.map((day) => holidays.filter((holiday) => sameDay(holiday.date, day))), [days, holidays]);
  const selectCalendarEventRef = useRef<(event: CalendarEvent) => void>(() => {});
  const selectCalendarEvent = useCallback((event: CalendarEvent) => selectCalendarEventRef.current(event), []);
  const toggleMonthDay = useCallback((day: Date) => {
    setMonthDay((current) => current && sameDay(current, day) ? null : day);
  }, []);
  const openMonthDay = useCallback((day: Date) => setMonthDay(day), []);
  const mailboxColor = useCallback((mailboxId: number) => {
    const mailbox = mailboxes.find((item) => item.id === mailboxId);
    return mailbox ? accountColor(mailbox) : defaultAccountColor(mailboxId);
  }, [mailboxes]);
  const scheduledInterviewsByAccount = useMemo(() => mailboxes.filter((mailbox) => mailbox.is_active && !hiddenMailboxIds.includes(mailbox.id)).map((mailbox) => ({
    mailboxId: mailbox.id,
    email: mailbox.email_address,
    color: accountColor(mailbox),
    count: shownEvents.filter((event) => event.mailbox_id === mailbox.id && event.kind === "confirmed").length,
  })), [shownEvents, mailboxes, hiddenMailboxIds]);
  const exportableInterviews = useMemo(
    () => shownEvents.filter((event) => event.kind === "confirmed" && event.origin === "email"),
    [shownEvents],
  );
  const exportInterviews = useCallback(() => {
    if (!exportableInterviews.length) return;
    const blob = new Blob(["\ufeff", interviewCsv(exportableInterviews, timeZone)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "interviews.csv";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }, [exportableInterviews, timeZone]);
  const nextMeeting = useMemo(() => shownEvents.filter((event) => event.kind === "confirmed" && new Date(event.end_at).getTime() > Date.now()).sort((a, b) => new Date(a.start_at).getTime() - new Date(b.start_at).getTime())[0] ?? null, [shownEvents]);
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
    // The month agenda belongs to the day that opened it. Once a chip (or an
    // item inside that agenda) is selected, leaving the old agenda mounted
    // makes it look anchored to the newly selected event's day even though it
    // still contains the previous day's events.
    setMonthDay(null);
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
  selectCalendarEventRef.current = showEventDetails;
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
    const payload: CalendarEventPayload = { mailbox_id: editor.mailbox_id, title: editor.title, company: editor.company, role: editor.role, interviewer: editor.interviewer, work_model: editor.work_model, start_at: startAt.toISOString(), end_at: endAt.toISOString(), source_timezone: timeZone, description: editor.description, job_url: editor.job_url || null, meeting_type: editor.meeting_type, meeting_provider: editor.meeting_provider || null, meeting_url: editor.meeting_url || null, phone_number: editor.phone_number || null, phone_access_code: editor.phone_access_code || null, interview_status: editor.interview_status ?? "confirmation", application_status: editor.application_status };
    try { const saved = editor.id ? await updateCalendarEvent(editor.id, payload) : await createCalendarEvent(payload); CALENDAR_RANGE_CACHE.clear(); setEvents((prev) => [...prev.filter((x) => x.id !== saved.id), saved]); showEventDetails(saved); setEditor(null); }
    catch (err) { setSaveError(err instanceof Error ? err.message : "Failed to save event"); }
    finally { setSaving(false); }
  };

  const confirmInterviewStatus = async (next: ConfirmableStatus, applicationStatus?: CalendarEvent["application_status"]) => {
    if (!selected || selected.origin !== "manual" || selected.kind !== "confirmed" || statusSaving || cardStatus(selected).key === next) return;
    setStatusSaving(true);
    setStatusError("");
    const payload: CalendarEventPayload = next === "canceled"
      ? { interview_status: "cancellation" }
      : {
          application_status: next === "rejected_closed" ? (applicationStatus === "position_closed" ? "position_closed" : "rejected") : next,
          ...(selected.interview_status === "cancellation" ? { interview_status: "confirmation" as const } : {}),
        };
    try {
      const saved = await updateCalendarEvent(selected.id, payload);
      CALENDAR_RANGE_CACHE.clear();
      setEvents((prev) => prev.map((item) => item.id === saved.id ? saved : item));
      setSelected((current) => current?.id === saved.id ? saved : current);
    } catch (err) {
      setStatusError(err instanceof Error ? err.message : "Failed to confirm interview status");
    } finally {
      setStatusSaving(false);
    }
  };

  const deleteSelectedEvent = async () => {
    if (!selected || selected.origin !== "manual" || deleting) return;
    if (!window.confirm(`Delete “${selected.title}”? This cannot be undone.`)) return;
    setDeleting(true);
    setStatusError("");
    try {
      await deleteCalendarEvent(selected.id);
      CALENDAR_RANGE_CACHE.clear();
      setEvents((current) => current.filter((event) => event.id !== selected.id));
      setSelected(null);
    } catch (err) {
      setStatusError(err instanceof Error ? err.message : "Failed to delete event");
    } finally {
      setDeleting(false);
    }
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

  const selectedEventId = selected && !detailsClosing ? selected.id : null;
  const selectedStatus = selected ? cardStatus(selected) : null;
  const title = periodTitle(anchor, days, view);
  const weekCrossesMonth = view !== "month" && (days[0].getUTCMonth() !== days[days.length - 1].getUTCMonth() || days[0].getUTCFullYear() !== days[days.length - 1].getUTCFullYear());

  const clampPanelWidth = (width: number) => {
    const available = calendarMainRef.current?.getBoundingClientRect().width ?? window.innerWidth;
    const maximum = Math.max(CALENDAR_PANEL_MIN, Math.min(CALENDAR_PANEL_MAX, available - CALENDAR_BOARD_MIN - 6));
    return Math.round(Math.min(maximum, Math.max(CALENDAR_PANEL_MIN, width)));
  };

  const beginPanelResize = (event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    panelResizeRef.current = { startX: event.clientX, startWidth: panelWidth };
    setPanelResizing(true);
  };

  const updatePanelResize = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = panelResizeRef.current;
    if (!drag) return;
    const next = clampPanelWidth(drag.startWidth - (event.clientX - drag.startX));
    panelWidthRef.current = next;
    setPanelWidth(next);
  };

  const finishPanelResize = () => {
    if (!panelResizeRef.current) return;
    panelResizeRef.current = null;
    setPanelResizing(false);
    try { localStorage.setItem(CALENDAR_PANEL_WIDTH_KEY, String(panelWidthRef.current)); } catch { /* ignore unavailable storage */ }
  };

  const resizePanelWithKeyboard = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const next = clampPanelWidth(panelWidth + (event.key === "ArrowLeft" ? 24 : -24));
    panelWidthRef.current = next;
    setPanelWidth(next);
    try { localStorage.setItem(CALENDAR_PANEL_WIDTH_KEY, String(next)); } catch { /* ignore unavailable storage */ }
  };

  const resetPanelWidth = () => {
    const next = clampPanelWidth(defaultCalendarPanelWidth());
    panelWidthRef.current = next;
    setPanelWidth(next);
    try { localStorage.setItem(CALENDAR_PANEL_WIDTH_KEY, String(next)); } catch { /* ignore unavailable storage */ }
  };

  return <main className={`calendar-workspace ${sidebarOpen ? "sidebar-open" : ""} ${sidebarClosing ? "sidebar-closing" : ""} ${detailsClosing ? "details-closing" : ""} ${panelResizing ? "panel-resizing" : ""}`}>
    <header className="calendar-toolbar">
      <div className="calendar-title-group"><button className="calendar-back" type="button" onClick={onClose} aria-label="Back to inbox" data-tooltip="Back to inbox" data-tooltip-placement="bottom"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5 8 12l7 7M8 12h11" /></svg></button><h1><strong>{title.primary}</strong>{title.secondary && <span>{title.secondary}</span>}</h1></div>
      <div className="calendar-nav"><button type="button" onClick={() => move(-1)} aria-label="Previous period">‹</button><button className="calendar-today" type="button" onClick={() => setAnchor(startOfDay(wallDateForInstant(new Date(), timeZone)))}>Today</button><button type="button" onClick={() => move(1)} aria-label="Next period">›</button></div>
      <div className="calendar-toolbar-actions"><CalendarTimeZonePicker value={timeZone} onChange={setTimeZone} /><CalendarViewPicker value={view} onChange={setView} /><button className="calendar-sidebar-toggle" type="button" aria-label={sidebarOpen ? "Hide calendar sidebar" : "Show calendar sidebar"} title={sidebarOpen ? "Hide sidebar" : "Show sidebar"} aria-pressed={sidebarOpen} onClick={toggleSidebar}>▥</button></div>
    </header>
    {error && <div className="calendar-state error"><span>{error}</span><button type="button" onClick={() => void load()}>Retry</button></div>}
    {statusFilters.includes("available") && (
      <PendingInterviewsAlert
        items={pendingInterviews.filter((item) => !hiddenMailboxIds.includes(item.mailbox_id))}
        timeZone={timeZone}
        mailboxColor={mailboxColor}
        onJump={(value) => {
          const wall = wallDateForInstant(new Date(value), timeZone);
          setAnchor(new Date(Date.UTC(wall.getUTCFullYear(), wall.getUTCMonth(), wall.getUTCDate())));
        }}
      />
    )}
    <div ref={calendarMainRef} className={`calendar-main ${selected ? "has-details" : ""}`} style={{ "--calendar-details-width": `${panelWidth}px` } as React.CSSProperties}>
      <div className="calendar-board">
      {view === "month" ? (
        <>
        <section className="calendar-grid month" aria-busy={loading || refreshing}>
          {VISIBLE_WEEKDAYS.map((day) => <div className="calendar-weekday" key={day}>{day}</div>)}
        {days.map((day, dayIndex) => <MonthDayCell
            key={day.toISOString()}
            day={day}
            events={dayGroups[dayIndex].all}
            holidays={holidaysByDay[dayIndex] ?? []}
            today={sameDay(day, nowInTimeZone)}
            outside={day.getUTCMonth() !== anchor.getUTCMonth()}
            selected={monthDay != null && sameDay(day, monthDay)}
            selectedEventId={selectedEventId}
            timeZone={timeZone}
            colorFor={mailboxColor}
            onSelectEvent={selectCalendarEvent}
            onToggle={toggleMonthDay}
            onOpen={openMonthDay}
          />)}
        </section>
        {monthDay && <MonthDayAgenda
          day={monthDay}
          events={dayGroups[days.findIndex((day) => sameDay(day, monthDay))]?.all ?? []}
          holidays={holidaysByDay[days.findIndex((day) => sameDay(day, monthDay))] ?? []}
          timeZone={timeZone}
          colorFor={(event) => mailboxColor(event.mailbox_id)}
          onClose={() => setMonthDay(null)}
          onSelect={showEventDetails}
          onCreate={defaultMailboxId == null ? null : () => { const day = monthDay; setMonthDay(null); openCreate(day); }}
        />}
        </>
      ) : (
        <section ref={timelineRef} className={`calendar-time-grid ${view}`} aria-busy={loading || refreshing} aria-label={`${view} calendar. Drag on a time range to create an event.`} style={{ "--day-count": days.length, "--hour-height": `${hourHeight}px`, "--day-height": `${hourHeight * HOURS.length}px` } as React.CSSProperties}>
          <div ref={timelineHeaderRef} className="calendar-time-header">
            <div className="calendar-time-corner" />
            {days.map((day) => { const today = sameDay(day, nowInTimeZone); const weekday = new Intl.DateTimeFormat(undefined, { timeZone: "UTC", weekday: "short" }).format(day); const month = new Intl.DateTimeFormat(undefined, { timeZone: "UTC", month: "short" }).format(day); return <div className={`calendar-time-day-heading ${today ? "today" : ""}`} key={day.toISOString()}><span>{weekCrossesMonth ? `${weekday} ${month}` : weekday}</span><strong>{day.getUTCDate()}</strong></div>; })}
            <div className="calendar-all-day-label">all-day</div>
          {days.map((day, dayIndex) => <div className="calendar-all-day-cell" key={`all-day-${day.toISOString()}`}>{holidaysByDay[dayIndex]?.map((holiday) => <HolidayChip holiday={holiday} key={holiday.id} />)}{dayGroups[dayIndex]?.allDay.map((event) => <EventCard event={event} color={mailboxColor(event.mailbox_id)} timeZone={timeZone} compact selected={selectedEventId === event.id} onSelect={selectCalendarEvent} key={event.id} />)}</div>)}
          </div>
          <div className="calendar-time-body">
            <div className="calendar-time-axis">{HOURS.map((hour) => <span key={hour} style={{ top: `${hour * hourHeight}px` }}>{hourLabel(hour)}</span>)}</div>
            <div className="calendar-time-days">
            {days.map((day, dayIndex) => { const dayEvents = dayGroups[dayIndex]?.timed ?? []; const today = sameDay(day, nowInTimeZone); const nowMinutes = nowInTimeZone.getUTCHours() * 60 + nowInTimeZone.getUTCMinutes(); return <div
                className={`calendar-time-day ${today ? "today" : ""} ${defaultMailboxId == null ? "disabled" : ""}`}
                key={day.toISOString()}
                title={defaultMailboxId == null ? "Connect an email account to create events" : undefined}
                onPointerDown={(event) => beginTimeDrag(dayIndex, event)}
                onPointerMove={(event) => updateTimeDrag(dayIndex, event)}
                onPointerUp={(event) => finishTimeDrag(dayIndex, event)}
                onPointerCancel={cancelTimeDrag}
              >
                {HOURS.map((hour) => <div className="calendar-hour-slot" key={hour} />)}
                {today && <div className="calendar-now-line" style={{ top: `${(nowMinutes / 60) * hourHeight}px` }} />}
                {dragSelection?.dayIndex === dayIndex && <div className="calendar-drag-selection" style={{ top: `${(dragSelection.startMinutes / 60) * hourHeight}px`, height: `${((dragSelection.endMinutes - dragSelection.startMinutes) / 60) * hourHeight}px` }}><span>{formatWallTime(new Date(Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate(), 0, dragSelection.startMinutes)))} – {formatWallTime(new Date(Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate(), 0, dragSelection.endMinutes)))}</span></div>}
                {layoutTimedEvents(dayEvents, day, hourHeight, timeZone).map(({ event, position }) => <EventCard event={event} color={mailboxColor(event.mailbox_id)} timeZone={timeZone} timed position={position} selected={selectedEventId === event.id} onSelect={selectCalendarEvent} key={event.id} />)}
              </div>; })}
            </div>
          </div>
        </section>
      )}
      {loading && <CalendarLoading />}
      {!loading && refreshing && <CalendarLoading soft />}
      </div>
      {(selected || sidebarOpen || sidebarClosing) && <button className="calendar-panel-scrim" type="button" aria-label="Close calendar panel" onClick={selected ? closeEventDetails : toggleSidebar} />}
      {(selected || sidebarOpen || sidebarClosing) && <button
        className="calendar-panel-splitter"
        type="button"
        role="separator"
        aria-label="Resize calendar side panel"
        aria-orientation="vertical"
        aria-valuemin={CALENDAR_PANEL_MIN}
        aria-valuemax={CALENDAR_PANEL_MAX}
        aria-valuenow={panelWidth}
        title="Drag to resize; double-click to reset"
        onPointerDown={beginPanelResize}
        onPointerMove={updatePanelResize}
        onPointerUp={finishPanelResize}
        onPointerCancel={finishPanelResize}
        onKeyDown={resizePanelWithKeyboard}
        onDoubleClick={resetPanelWidth}
      />}
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
            <div className="details-row details-time-row"><dt>Interview time</dt><dd><InterviewTimeRange event={selected} timeZone={timeZone} /></dd></div>
            <div className="details-row"><dt>Account</dt><dd>{selected.email_account}</dd></div>
            <div className="details-row"><dt>Company</dt><dd>{selected.company || "—"}</dd></div>
            <div className="details-row"><dt>Role</dt><dd>{selected.role || "—"}</dd></div>
            <div className={`details-row details-type-row meeting-${interviewType(selected).type}`}><dt>Interview type</dt><dd><MeetingTypeIcon type={interviewType(selected).type} /><span>{interviewType(selected).label}</span></dd></div>
            {selected.kind === "confirmed" && selectedStatus && selectedStatus.key !== "available" && selectedStatus.key !== "active" && <div className={`details-row details-status-row ${selectedStatus.className}`}><dt>Interview status</dt><dd>{selectedStatus.key === "canceled" ? <CanceledStatusIcon /> : <ApplicationStatusIcon status={selectedStatus.key === "rejected_closed" ? "rejected" : selectedStatus.key === "passed" ? "passed" : "active"} />}<span>{selectedStatus.label}</span></dd></div>}
            {interviewType(selected).type === "video" && <div className="details-row details-action-row"><dt>Meeting</dt><dd>{meetingProviderLabel(selected) !== "—" ? <span className="details-meeting-value">{selected.meeting_url ? <a className="details-meeting-link" href={selected.meeting_url} target="_blank" rel="noreferrer" aria-label={`Open ${meetingProviderLabel(selected)}`} title={`Open ${meetingProviderLabel(selected)}`}><span className="details-meeting-icon"><MeetingTypeIcon type="video" /></span><span>{meetingProviderLabel(selected)}</span><svg className="details-meeting-arrow" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3h7v7M13 3 5 11" /></svg></a> : <span className="details-meeting-label"><span className="details-meeting-icon"><MeetingTypeIcon type="video" /></span><span>{meetingProviderLabel(selected)}</span></span>}{selected.meeting_url && <button className="details-copy-link details-copy-icon" type="button" onClick={() => void copyMeetingLink(selected)} aria-label={copiedMeetingEventId === selected.id ? "Meeting link copied" : `Copy ${meetingProviderLabel(selected)} meeting link`} title={copiedMeetingEventId === selected.id ? "Copied" : "Copy meeting link"}>{copiedMeetingEventId === selected.id ? <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 8.2 3 3 6-6.4" /></svg> : <svg viewBox="0 0 16 16" aria-hidden="true"><rect x="5.25" y="2.25" width="8.5" height="8.5" rx="1.5" /><path d="M10.75 10.75v1.5a1.5 1.5 0 0 1-1.5 1.5h-5.5a1.5 1.5 0 0 1-1.5-1.5v-5.5a1.5 1.5 0 0 1 1.5-1.5h1.5" /></svg>}</button>}</span> : "—"}</dd></div>}
          </dl>
          {statusError && <p className="error-text">{statusError}</p>}

          {selected.kind === "confirmed" && selected.origin === "manual" && <div className="details-actions">
            <button className="details-secondary-action" type="button" onClick={() => setEditor(editorFor(new Date(selected.start_at), selected.mailbox_id, timeZone, selected))}><svg viewBox="0 0 16 16" aria-hidden="true"><path d="m10.8 2.7 2.5 2.5-7.7 7.7-3 .5.5-3 7.7-7.7ZM9.4 4.1l2.5 2.5" /></svg>Edit event</button>
            <button className="details-secondary-action is-danger" type="button" disabled={deleting} onClick={() => void deleteSelectedEvent()}><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 4h10M6 4V2.8h4V4m1.7 0-.6 9H4.9l-.6-9M6.5 6.5v4.2m3-4.2v4.2" /></svg>{deleting ? "Deleting…" : "Delete event"}</button>
          </div>}

          {selected.origin === "email" && <section className="source-email">
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
                const messageStatus = selected.kind === "confirmed" ? emailInterviewStatus(message) : null;
                const statusMatches = messageStatus != null && selectedStatus?.key === messageStatus.key;
                return <li key={message.id}>
                  {messageStatus && !(messageStatus.key === "active" && statusMatches) && <div className="source-email-status-bar"><span className={`source-email-status ${messageStatus.className}`}>{messageStatus.label}</span>{statusMatches || selected.origin === "email" ? <span className="source-email-status-match">{statusMatches ? "Confirmed for this interview" : "Detected from email"}</span> : <button type="button" disabled={statusSaving} onClick={() => void confirmInterviewStatus(messageStatus.key, messageStatus.applicationStatus)}>Confirm this status</button>}</div>}
                  <button className="source-email-list-item" type="button" aria-expanded={expanded} onClick={() => {
                    if (expanded) {
                      setExpandedEmailId(null);
                      return;
                    }
                    setExpandedEmailId(message.id);
                    if (message.body_html) return;
                    void fetchEmailDetail(message.id, false).then((detail) => {
                      setEmailMessages((current) => current.map((item) => item.id === detail.id ? { ...item, ...detail } : item));
                    }).catch(() => {});
                  }}>
                    <span className="source-email-list-top"><strong>{message.subject || "(no subject)"}</strong><time dateTime={timestamp}>{formatDateTime(timestamp, timeZone)}</time></span>
                    <span className="source-email-meta">{message.sender}{message.interview_subtype && <><span>·</span>{message.interview_subtype.replaceAll("_", " ")}</>}</span>
                    <span className="source-email-snippet">{decodeMailText(message.snippet || message.body_text)}</span>
                  </button>
                  {expanded && <div className="source-email-body"><EmailBody html={message.body_html} text={message.body_text} snippet={message.snippet} /></div>}
                </li>;
              })}
            </ol>}
          </section>}
        </div>
      </aside> : <aside className="calendar-sidebar">
        <section className="calendar-status-card">
          <h2>Interview view</h2>
          <div className="calendar-status-options" role="group" aria-label="Interview view">
            {CALENDAR_STATUS_FILTERS.map((item) => (
              <label className="calendar-status-option" key={item.value}>
                <input
                  type="checkbox"
                  checked={statusFilters.includes(item.value)}
                  onChange={() => {
                    setStatusFilters((current) => {
                      const next = current.includes(item.value)
                        ? current.filter((value) => value !== item.value)
                        : [...current, item.value];
                      persistStatusFilters(next);
                      return next;
                    });
                  }}
                />
                <span className={`calendar-status-swatch is-${item.value}`} aria-hidden="true" />
                <span>{item.label}</span>
              </label>
            ))}
          </div>
        </section>
        <section className="calendar-mini-month">
          <header><strong>{new Intl.DateTimeFormat(undefined, { timeZone: "UTC", month: "long", year: "numeric" }).format(anchor)}</strong><div><button type="button" aria-label="Previous month" onClick={() => setAnchor(new Date(Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth() - 1, 1)))}>‹</button><button type="button" aria-label="Next month" onClick={() => setAnchor(new Date(Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth() + 1, 1)))}>›</button></div></header>
          <div className="mini-weekdays">{VISIBLE_WEEKDAYS.map((day) => <span key={day}>{day.slice(0, 2)}</span>)}</div>
          <div className="mini-days">{miniMonthDays.map((day) => <button type="button" className={`${day.getUTCMonth() !== anchor.getUTCMonth() ? "outside" : ""} ${view === "week" && day >= start && day < end ? "in-range" : ""} ${sameDay(day, anchor) ? "selected" : ""} ${sameDay(day, nowInTimeZone) ? "today" : ""}`} key={day.toISOString()} onClick={() => setAnchor(day)}>{day.getUTCDate()}</button>)}</div>
        </section>
        <section className="calendar-next-meeting"><h2>Next meeting</h2>{nextMeeting ? <button type="button" className="next-meeting-card" onClick={() => showEventDetails(nextMeeting)}><span className="next-meeting-logo" style={{ "--account-color": mailboxColor(nextMeeting.mailbox_id) } as React.CSSProperties}>{eventInitials(nextMeeting)}</span><span><strong>{nextMeeting.title}</strong><small>{startsIn(nextMeeting.start_at)}</small></span></button> : <p>No upcoming meetings in this view.</p>}</section>
        <ScheduledInterviewChart accounts={scheduledInterviewsByAccount} exportCount={exportableInterviews.length} onExport={exportInterviews} />
        <section className="calendar-legend">
          <header>
            <h2>Calendars</h2>
            {activeMailboxes.length > 1 && (
              <button type="button" onClick={() => updateHiddenMailboxIds(() => allCalendarsShown ? activeMailboxes.map((mailbox) => mailbox.id) : [])}>
                {allCalendarsShown ? "Hide all" : "Show all"}
              </button>
            )}
          </header>
          <div className="calendar-legend-options" role="group" aria-label="Calendars">
            {activeMailboxes.map((mailbox) => (
              <label className="calendar-legend-option" key={mailbox.id} title={mailbox.email_address} style={{ "--account-color": accountColor(mailbox) } as React.CSSProperties}>
                <input
                  type="checkbox"
                  checked={!hiddenMailboxIds.includes(mailbox.id)}
                  onChange={() => updateHiddenMailboxIds((current) => current.includes(mailbox.id) ? current.filter((id) => id !== mailbox.id) : [...current, mailbox.id])}
                />
                <span>{mailbox.email_address}</span>
              </label>
            ))}
          </div>
        </section>
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
      <label>Interview type<CompactSelect value={editor.meeting_type} onChange={(e) => setEditor({ ...editor, meeting_type: e.target.value as Editor["meeting_type"] })}><option value="unspecified">Not specified</option><option value="video">Video call</option><option value="phone">Phone call</option><option value="in_person">In-person interview</option></CompactSelect></label>
      {editor.meeting_type === "video" && <div className="editor-row"><label>Provider<input placeholder="Zoom, Google Meet…" value={editor.meeting_provider} onChange={(e) => setEditor({ ...editor, meeting_provider: e.target.value })} /></label><label>HTTPS meeting link<input type="url" value={editor.meeting_url} onChange={(e) => setEditor({ ...editor, meeting_url: e.target.value })} /></label></div>}
      {editor.meeting_type === "phone" && <div className="editor-row"><label>Phone<input type="tel" value={editor.phone_number} onChange={(e) => setEditor({ ...editor, phone_number: e.target.value })} /></label><label>Access code<input value={editor.phone_access_code} onChange={(e) => setEditor({ ...editor, phone_access_code: e.target.value })} /></label></div>}
      <label>Job link<input type="url" value={editor.job_url} onChange={(e) => setEditor({ ...editor, job_url: e.target.value })} /></label><label>Description<textarea rows={4} value={editor.description} onChange={(e) => setEditor({ ...editor, description: e.target.value })} /></label>{saveError && <p className="error-text">{saveError}</p>}<footer><button type="button" onClick={() => setEditor(null)}>Cancel</button><button className="primary" disabled={saving}>{saving ? "Saving…" : "Save event"}</button></footer>
    </form></div>}
  </main>;
}
