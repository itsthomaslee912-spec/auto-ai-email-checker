import type { ButtonHTMLAttributes, ReactNode } from "react";

export type UiIconName =
  | "back"
  | "plus"
  | "close"
  | "reply"
  | "send"
  | "spark"
  | "paperclip"
  | "trash"
  | "search"
  | "refresh"
  | "eye"
  | "eye-off"
  | "chevron-left"
  | "chevron-right"
  | "today"
  | "snooze"
  | "mute"
  | "unmute"
  | "bell"
  | "check"
  | "list"
  | "table"
  | "pane-none"
  | "pane-right"
  | "pane-below"
  | "inbox"
  | "unread"
  | "monitor"
  | "moon"
  | "sun"
  | "mail"
  | "sliders"
  | "chart"
  | "link"
  | "video"
  | "phone"
  | "clock";

const ICONS: Record<UiIconName, ReactNode> = {
  back: <path d="M15 5 8 12l7 7M8 12h11" />,
  plus: <path d="M12 5v14M5 12h14" />,
  close: <path d="M6 6l12 12M18 6 6 18" />,
  reply: <><path d="M9 8 4 12l5 4" /><path d="M4 12h9.5a5.5 5.5 0 0 1 0 11H12" /></>,
  send: <path d="m4 12 16-8-6 16-2.5-6.5z" />,
  spark: <><path d="M12 3.5 13.6 8 18 9.6 13.6 11.2 12 15.7 10.4 11.2 6 9.6 10.4 8z" /><path d="m18 14.5.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z" /></>,
  paperclip: <path d="M8.5 12.5 14 7a3 3 0 0 1 4.2 4.2l-7.2 7.2a4.2 4.2 0 0 1-6-6L12.2 5.2" />,
  trash: <path d="M4.5 7h15M9 7V5h6v2M7 7l.8 12h8.4L17 7" />,
  search: <><circle cx="11" cy="11" r="6" /><path d="m16 16 4 4" /></>,
  refresh: <><path d="M20 7v5h-5M4 17v-5h5" /><path d="M6.1 8.2A7 7 0 0 1 18.8 7L20 12M4 12l1.2 5A7 7 0 0 0 17.9 15.8" /></>,
  eye: <><path d="M2.5 12S6 6.5 12 6.5 21.5 12 21.5 12 18 17.5 12 17.5 2.5 12 2.5 12Z" /><circle cx="12" cy="12" r="2.4" /></>,
  "eye-off": <><path d="M4 5.5 19 19" /><path d="M9.2 9.4A3 3 0 0 0 14.6 15M7.2 7.6C5 9 3.2 12 3.2 12S6.4 17.5 12 17.5c1.3 0 2.5-.3 3.5-.8M10.2 6.7A9 9 0 0 1 12 6.5C18 6.5 21.5 12 21.5 12a16 16 0 0 1-2.2 2.8" /></>,
  "chevron-left": <path d="m14.5 6-5 6 5 6" />,
  "chevron-right": <path d="m9.5 6 5 6-5 6" />,
  today: <><rect x="4" y="5" width="16" height="15" rx="2" /><path d="M8 3.5V7M16 3.5V7M4 10h16" /><circle cx="12" cy="15" r="1.3" /></>,
  snooze: <><circle cx="12" cy="13" r="7" /><path d="M12 10v3.2l2.2 1.4M9 4.5h4.5L9 8h4.5" /></>,
  mute: <path d="M4 10v4h3l4 3V7L7 10zM16 10l4 4M20 10l-4 4" />,
  unmute: <path d="M4 10v4h3l4 3V7L7 10zM16 9.5a4 4 0 0 1 0 5M18.2 7.2a7 7 0 0 1 0 9.6" />,
  bell: <><path d="M6 16.5h12l-1.2-2.1V11a4.8 4.8 0 0 0-9.6 0v3.4z" /><path d="M10 16.5a2 2 0 0 0 4 0" /></>,
  check: <path d="m5 12.5 4.2 4.2L19 7.5" />,
  list: <path d="M8 7h12M8 12h12M8 17h12M4 7h.01M4 12h.01M4 17h.01" />,
  table: <><rect x="4" y="5" width="16" height="14" rx="2" /><path d="M4 10h16M4 15h16M10 5v14" /></>,
  "pane-none": <rect x="5" y="5" width="14" height="14" rx="2" />,
  "pane-right": <><rect x="4" y="5" width="16" height="14" rx="2" /><path d="M13 5v14" /></>,
  "pane-below": <><rect x="4" y="5" width="16" height="14" rx="2" /><path d="M4 13h16" /></>,
  inbox: <><path d="M4 13h4l1.2 2h5.6L16 13h4" /><path d="M6 6h12l2 7v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-5z" /></>,
  unread: <><path d="M4 7.5 12 13l8-5.5" /><path d="M4 7h16v10H4z" /><circle cx="18.5" cy="6.5" r="2.2" /></>,
  monitor: <><rect x="3.5" y="5" width="17" height="11" rx="2" /><path d="M9 19.5h6M12 16v3.5" /></>,
  moon: <path d="M16 14.5A6.5 6.5 0 0 1 9.2 5 6.5 6.5 0 1 0 16 14.5z" />,
  sun: <><circle cx="12" cy="12" r="3.2" /><path d="M12 3.5v2.2M12 18.3v2.2M3.5 12h2.2M18.3 12h2.2M6 6l1.6 1.6M16.4 16.4 18 18M18 6l-1.6 1.6M7.6 16.4 6 18" /></>,
  mail: <><rect x="3.5" y="5.5" width="17" height="13" rx="2" /><path d="m4.5 8 7.5 5 7.5-5" /></>,
  sliders: <><path d="M4 8h16M4 16h16" /><circle cx="9" cy="8" r="2" /><circle cx="15" cy="16" r="2" /></>,
  chart: <><path d="M4 19V5M4 19h16" /><path d="M8 15v-3M12 15V8M16 15v-5" /></>,
  link: <><path d="M10 13a4 4 0 0 0 5.7.3l2-2a4 4 0 0 0-5.7-5.6L11 6.8" /><path d="M14 11a4 4 0 0 0-5.7-.3l-2 2a4 4 0 0 0 5.7 5.6L13 17.2" /></>,
  video: <><rect x="3.5" y="6.5" width="12" height="11" rx="2" /><path d="m15.5 10 5-2.5v9L15.5 14" /></>,
  phone: <path d="M8 4.5h2.2l1.2 3-1.6 1a11 11 0 0 0 5.2 5.2l1-1.6 3 1.2V16a2 2 0 0 1-2.2 2A13.5 13.5 0 0 1 6 7.7 2 2 0 0 1 8 4.5z" />,
  clock: <><circle cx="12" cy="12" r="7.5" /><path d="M12 8.5V12l2.5 2" /></>,
};

export function UiIcon({ name, className }: { name: UiIconName; className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
      {ICONS[name]}
    </svg>
  );
}

type IconButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  name: UiIconName;
  label: string;
};

export function IconButton({ name, label, className, type = "button", ...props }: IconButtonProps) {
  return (
    <button
      type={type}
      className={["icon-btn", className].filter(Boolean).join(" ")}
      aria-label={label}
      data-tooltip={label}
      data-tooltip-placement="bottom"
      {...props}
    >
      <UiIcon name={name} />
    </button>
  );
}
