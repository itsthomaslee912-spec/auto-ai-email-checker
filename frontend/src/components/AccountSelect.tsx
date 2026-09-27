import { useEffect, useId, useRef, useState } from "react";
import type { Mailbox } from "../api";

export default function AccountSelect({
  value,
  mailboxes,
  onChange,
}: {
  value: number | "all";
  mailboxes: Mailbox[];
  onChange: (value: number | "all") => void;
}) {
  const fieldId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<{ top: number; left: number; width: number; maxHeight: number } | null>(null);
  const selected = value === "all" ? null : mailboxes.find((box) => box.id === value);
  const label = selected?.email_address ?? "All accounts";

  useEffect(() => {
    if (!open) return;
    const rect = buttonRef.current?.getBoundingClientRect();
    if (rect) {
      const width = 264;
      const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
      setAnchor({
        top: rect.bottom + 6,
        left,
        width,
        maxHeight: Math.max(120, window.innerHeight - rect.bottom - 16),
      });
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
  }, [open]);

  function choose(next: number | "all") {
    setOpen(false);
    onChange(next);
  }

  return (
    <div className="modern-field" ref={rootRef}>
      <span className="modern-field-label" id={fieldId}>
        Account
      </span>
      <button
        ref={buttonRef}
        type="button"
        className={open ? "modern-control open" : "modern-control"}
        aria-labelledby={fieldId}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="modern-control-value">{label}</span>
        <svg viewBox="0 0 24 24" aria-hidden="true" className="modern-icon">
          <path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
      </button>
      {open && (
        <div
          className="modern-popover account-menu"
          role="listbox"
          aria-labelledby={fieldId}
          style={
            anchor
              ? {
                  position: "fixed",
                  top: anchor.top,
                  left: anchor.left,
                  width: anchor.width,
                  minWidth: anchor.width,
                  maxWidth: anchor.width,
                  boxSizing: "border-box",
                  maxHeight: anchor.maxHeight,
                }
              : undefined
          }
        >
          <button
            type="button"
            role="option"
            aria-selected={value === "all"}
            className={value === "all" ? "account-option selected" : "account-option"}
            onClick={() => choose("all")}
          >
            All accounts
          </button>
          {mailboxes.map((box) => (
            <button
              key={box.id}
              type="button"
              role="option"
              aria-selected={value === box.id}
              className={value === box.id ? "account-option selected" : "account-option"}
              onClick={() => choose(box.id)}
            >
              {box.email_address}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
