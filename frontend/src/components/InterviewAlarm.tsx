import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { accountColor } from "../accountColor";
import { fetchCalendarEvents, openEvents, type CalendarEvent, type Mailbox } from "../api";

const LEAD_MS = 10 * 60 * 1000;
const GRACE_MS = 60 * 1000;
const CHIME_EVERY_MS = 8_000;
const HOLDS_KEY = "interview-alarm-holds";
const SOUND_KEY = "interview-alarm-sound";

type Hold = { kind: "dismiss" } | { kind: "snooze"; until: number };

function readHolds(): Record<string, Hold> {
  try {
    const parsed: unknown = JSON.parse(sessionStorage.getItem(HOLDS_KEY) || "{}");
    if (!parsed || typeof parsed !== "object") return {};
    return parsed as Record<string, Hold>;
  } catch {
    return {};
  }
}

function writeHolds(holds: Record<string, Hold>) {
  try { sessionStorage.setItem(HOLDS_KEY, JSON.stringify(holds)); }
  catch { /* ignore unavailable storage */ }
}

function soundEnabled() {
  try { return localStorage.getItem(SOUND_KEY) !== "off"; }
  catch { return true; }
}

function dueInterviews(events: CalendarEvent[], now: number, holds: Record<string, Hold>) {
  return events
    .filter((event) => {
      if (event.kind !== "confirmed") return false;
      if (event.interview_status === "cancellation") return false;
      if (event.application_status !== "active") return false;
      const start = new Date(event.start_at).getTime();
      if (!Number.isFinite(start)) return false;
      const remaining = start - now;
      if (remaining > LEAD_MS || remaining <= -GRACE_MS) return false;
      const hold = holds[String(event.id)];
      if (!hold) return true;
      if (hold.kind === "dismiss") return false;
      return hold.until <= now;
    })
    .sort((a, b) => new Date(a.start_at).getTime() - new Date(b.start_at).getTime());
}

function countdown(ms: number) {
  const seconds = Math.max(0, Math.ceil(ms / 1000));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

function whenLabel(ms: number) {
  if (ms <= 0) return "Starting now";
  const minutes = Math.ceil(ms / 60_000);
  if (minutes <= 1) return "Starts in less than a minute";
  return `Starts in ${minutes} minutes`;
}

function clockLabel(value: string) {
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(new Date(value));
}

let audioContext: AudioContext | null = null;

function sharedAudio() {
  const Context = window.AudioContext || (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Context) return null;
  if (!audioContext) audioContext = new Context();
  return audioContext;
}

function playChime() {
  const ctx = sharedAudio();
  if (!ctx) return false;
  if (ctx.state === "suspended") {
    void ctx.resume();
    return false;
  }
  const start = ctx.currentTime;
  [523.25, 659.25, 783.99].forEach((frequency, index) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    const at = start + index * 0.16;
    osc.type = "sine";
    osc.frequency.value = frequency;
    gain.gain.setValueAtTime(0.0001, at);
    gain.gain.exponentialRampToValueAtTime(0.07, at + 0.03);
    gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.55);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(at);
    osc.stop(at + 0.58);
  });
  return true;
}

export default function InterviewAlarm({ mailboxes }: { mailboxes: Mailbox[] }) {
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [now, setNow] = useState(() => Date.now());
  const [holds, setHolds] = useState<Record<string, Hold>>(readHolds);
  const [soundOn, setSoundOn] = useState(soundEnabled);
  const [soundBlocked, setSoundBlocked] = useState(false);
  const notified = useRef(new Set<number>());
  const lastChime = useRef(0);
  const lastChimeEvent = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    const start = new Date(Date.now() - 2 * 60_000).toISOString();
    const end = new Date(Date.now() + 6 * 60 * 60_000).toISOString();
    try {
      setEvents(await fetchCalendarEvents(start, end));
    } catch {
      /* keep the last list if the refresh fails */
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 20_000);
    const source = openEvents();
    const onChange = () => { void refresh(); };
    source.addEventListener("calendar.changed", onChange);
    return () => {
      window.clearInterval(timer);
      source.removeEventListener("calendar.changed", onChange);
      source.close();
    };
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const upcoming = useMemo(() => dueInterviews(events, now, holds), [events, now, holds]);
  const event = upcoming[0] ?? null;
  const remaining = event ? new Date(event.start_at).getTime() - now : 0;
  const color = event
    ? accountColor(mailboxes.find((mailbox) => mailbox.id === event.mailbox_id) ?? { id: event.mailbox_id })
    : "var(--accent)";

  const saveHold = (id: number, hold: Hold) => {
    setHolds((current) => {
      const next = { ...current, [String(id)]: hold };
      writeHolds(next);
      return next;
    });
  };

  useEffect(() => {
    if (!event || !soundOn) return;
    if (lastChimeEvent.current !== event.id) {
      lastChimeEvent.current = event.id;
      lastChime.current = 0;
    }
    if (lastChime.current !== 0 && now - lastChime.current < CHIME_EVERY_MS) return;
    lastChime.current = now;
    const played = playChime();
    setSoundBlocked((current) => current === !played ? current : !played);
  }, [event, now, soundOn]);

  useEffect(() => {
    if (!event || Notification.permission !== "granted" || notified.current.has(event.id)) return;
    notified.current.add(event.id);
    const title = event.company || event.title || "Interview";
    const body = `${whenLabel(remaining)}${event.role ? ` · ${event.role}` : ""}`;
    try { new Notification(title, { body, tag: `interview-${event.id}` }); }
    catch { /* notifications are optional */ }
  }, [event, remaining]);

  if (!event) return null;

  const radius = 20;
  const circumference = 2 * Math.PI * radius;
  const progress = remaining <= 0 ? 1 : 1 - Math.min(LEAD_MS, remaining) / LEAD_MS;
  const dash = circumference * progress;
  const snoozeMinutes = remaining > 6 * 60_000 ? 5 : 2;
  const company = event.company || event.title || "Interview";
  const joinUrl = event.meeting_type === "phone" ? null : event.meeting_url;

  const enableSound = async () => {
    const ctx = sharedAudio();
    if (ctx && ctx.state === "suspended") await ctx.resume();
    setSoundOn(true);
    try { localStorage.setItem(SOUND_KEY, "on"); } catch { /* ignore */ }
    setSoundBlocked(!playChime());
    lastChime.current = Date.now();
  };

  const mute = () => {
    setSoundOn(false);
    try { localStorage.setItem(SOUND_KEY, "off"); } catch { /* ignore */ }
  };

  const askNotify = async () => {
    if (!("Notification" in window) || Notification.permission !== "default") return;
    const permission = await Notification.requestPermission();
    if (permission === "granted") {
      try { new Notification(company, { body: whenLabel(remaining), tag: `interview-${event.id}` }); }
      catch { /* ignore */ }
    }
  };

  return (
    <section className="interview-alarm" role="alertdialog" aria-labelledby="interview-alarm-title" aria-describedby="interview-alarm-when">
      <div className="interview-alarm-ring" style={{ "--alarm-color": color } as CSSProperties} aria-hidden="true">
        <svg viewBox="0 0 52 52">
          <circle cx="26" cy="26" r={radius} />
          <circle cx="26" cy="26" r={radius} style={{ strokeDasharray: `${dash} ${circumference}` }} />
        </svg>
        <strong>{countdown(remaining)}</strong>
      </div>
      <div className="interview-alarm-copy">
        <p id="interview-alarm-when">{whenLabel(remaining)}</p>
        <h2 id="interview-alarm-title">{company}</h2>
        <p>
          {event.role || "Scheduled interview"}
          <span>·</span>
          {clockLabel(event.start_at)}
          {upcoming.length > 1 && <><span>·</span>{upcoming.length - 1} more soon</>}
        </p>
      </div>
      <div className="interview-alarm-actions">
        {joinUrl && <a className="interview-alarm-join" href={joinUrl} target="_blank" rel="noreferrer" onClick={() => saveHold(event.id, { kind: "dismiss" })}>Join</a>}
        {event.meeting_type === "phone" && event.phone_number && <a className="interview-alarm-join" href={`tel:${event.phone_number}`} onClick={() => saveHold(event.id, { kind: "dismiss" })}>Call</a>}
        <button type="button" onClick={() => saveHold(event.id, { kind: "snooze", until: Date.now() + snoozeMinutes * 60_000 })}>Snooze {snoozeMinutes} min</button>
        {soundOn
          ? <button type="button" onClick={mute}>Mute</button>
          : <button type="button" onClick={() => { void enableSound(); }}>Unmute</button>}
        {"Notification" in window && Notification.permission === "default" && <button type="button" onClick={() => { void askNotify(); }}>Alert outside this tab</button>}
        <button type="button" className="interview-alarm-dismiss" onClick={() => saveHold(event.id, { kind: "dismiss" })} aria-label="Dismiss interview alarm">×</button>
      </div>
      {soundOn && soundBlocked && <button type="button" className="interview-alarm-unlock" onClick={() => { void enableSound(); }}>Tap to hear the alarm</button>}
    </section>
  );
}
