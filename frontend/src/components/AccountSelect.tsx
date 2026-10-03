import { useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
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
  const menuRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<{ top: number; left: number; minWidth: number; maxWidth: number; maxHeight: number } | null>(null);
  const selected = value === "all" ? null : mailboxes.find((box) => box.id === value);
  const label = selected?.email_address ?? "All accounts";

  useLayoutEffect(() => {
    if (!open) {
      setAnchor(null);
      return;
    }
    let frame = 0;
    const place = () => {
      const rect = buttonRef.current?.getBoundingClientRect();
      if (rect) {
        const maxWidth = Math.min(360, window.innerWidth - 16);
        const minWidth = Math.min(Math.max(rect.width, 220), maxWidth);
        const width = Math.min(Math.max(minWidth, menuRef.current?.offsetWidth ?? 0), maxWidth);
        const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
        const below = window.innerHeight - rect.bottom - 16;
        const above = rect.top - 16;
        const menuHeight = menuRef.current?.scrollHeight ?? 0;
        const flip = below < Math.min(menuHeight, 240) && above > below;
        const next = {
          top: flip ? Math.max(8, rect.top - 6 - Math.min(menuHeight, above)) : rect.bottom + 6,
          left,
          minWidth,
          maxWidth,
          maxHeight: Math.max(120, flip ? above : below),
        };
        setAnchor((current) =>
          current
          && Math.abs(current.top - next.top) < 0.5
          && Math.abs(current.left - next.left) < 0.5
          && Math.abs(current.minWidth - next.minWidth) < 0.5
          && Math.abs(current.maxWidth - next.maxWidth) < 0.5
          && Math.abs(current.maxHeight - next.maxHeight) < 0.5
            ? current
            : next,
        );
      }
      frame = window.requestAnimationFrame(place);
    };
    place();
    const onPointer = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setOpen(false);
      buttonRef.current?.focus();
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
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
      {open && createPortal(
        <div
          ref={menuRef}
          className="modern-popover account-menu"
          role="listbox"
          aria-labelledby={fieldId}
          style={
            anchor
              ? {
                  position: "fixed",
                  zIndex: 10000,
                  top: anchor.top,
                  left: anchor.left,
                  width: "max-content",
                  minWidth: anchor.minWidth,
                  maxWidth: anchor.maxWidth,
                  boxSizing: "border-box",
                  maxHeight: anchor.maxHeight,
                }
              : { position: "fixed", visibility: "hidden" }
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
        </div>,
        document.body,
      )}
    </div>
  );
}
