import { useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type MouseEvent as ReactMouseEvent } from "react";
import { createPortal } from "react-dom";
import type { EmailLabel, InterviewSubtype } from "../api";
import { CLASSIFY_LABELS, CLASSIFY_LABEL_TITLES, INTERVIEW_SUBTYPE_TITLES, LABEL_BAR_COLORS } from "../labels";

export type CategoryChoice = { label: EmailLabel; subtype: InterviewSubtype | null };

type Option = CategoryChoice & { title: string; key: string };

const OPTIONS: Option[] = CLASSIFY_LABELS.flatMap((label): Option[] =>
  label === "interview_scheduled"
    ? (Object.entries(INTERVIEW_SUBTYPE_TITLES) as [InterviewSubtype, string][]).map(([subtype, title]) => ({
      label,
      subtype,
      title,
      key: `${label}:${subtype}`,
    }))
    : [{ label, subtype: null, title: CLASSIFY_LABEL_TITLES[label], key: label }],
);

export default function CategoryPicker({
  current,
  subject,
  onPick,
  onClose,
}: {
  current: CategoryChoice;
  subject: string;
  onPick: (choice: CategoryChoice) => void;
  onClose: () => void;
}) {
  const initialIndex = useMemo(() => {
    const exact = OPTIONS.findIndex((option) => option.label === current.label && option.subtype === current.subtype);
    if (exact >= 0) return exact;
    return Math.max(0, OPTIONS.findIndex((option) => option.label === current.label));
  }, [current.label, current.subtype]);
  const [index, setIndex] = useState(initialIndex);
  const listRef = useRef<HTMLUListElement>(null);
  const pointerRef = useRef<{ x: number; y: number } | null>(null);
  const hoverOption = (optionIndex: number, event: ReactMouseEvent) => {
    const last = pointerRef.current;
    pointerRef.current = { x: event.clientX, y: event.clientY };
    if (last && last.x === event.clientX && last.y === event.clientY) return;
    if (!last) return;
    setIndex(optionIndex);
  };

  useEffect(() => {
    listRef.current?.focus();
  }, []);

  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-index="${index}"]`)?.scrollIntoView({ block: "nearest" });
  }, [index]);

  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      setIndex((value) => (value + step + OPTIONS.length) % OPTIONS.length);
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      setIndex(event.key === "Home" ? 0 : OPTIONS.length - 1);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onPick(OPTIONS[index]);
    } else if (event.key === "Escape" || event.key.toLowerCase() === "l") {
      event.preventDefault();
      onClose();
    }
    event.stopPropagation();
  };

  return createPortal(
    <div className="category-picker-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="category-picker" role="dialog" aria-modal="true" aria-labelledby="category-picker-title">
        <header>
          <h3 id="category-picker-title">Set category</h3>
          <p title={subject}>{subject || "(no subject)"}</p>
        </header>
        <ul
          ref={listRef}
          className="category-picker-list"
          role="listbox"
          tabIndex={0}
          aria-labelledby="category-picker-title"
          aria-activedescendant={`category-option-${OPTIONS[index].key}`}
          onKeyDown={onKeyDown}
        >
          {OPTIONS.map((option, optionIndex) => {
            const isCurrent = option.label === current.label && (option.subtype ?? null) === (current.subtype ?? null);
            const startsGroup = option.subtype != null && OPTIONS[optionIndex - 1]?.label !== option.label;
            return (
              <li key={option.key} role="none">
                {startsGroup && <span className="category-picker-group">{CLASSIFY_LABEL_TITLES.interview_scheduled}</span>}
                <span
                  id={`category-option-${option.key}`}
                  role="option"
                  aria-selected={optionIndex === index}
                  data-index={optionIndex}
                  className={`category-picker-option${optionIndex === index ? " is-highlighted" : ""}${option.subtype ? " is-subtype" : ""}`}
                  style={{ "--option-color": LABEL_BAR_COLORS[option.label] } as CSSProperties}
                  onMouseMove={(event) => hoverOption(optionIndex, event)}
                  onClick={() => onPick(option)}
                >
                  <span className="category-picker-swatch" aria-hidden="true" />
                  <span className="category-picker-title">{option.title}</span>
                  {isCurrent && <span className="category-picker-current">Current</span>}
                </span>
              </li>
            );
          })}
        </ul>
        <footer>
          <span><kbd>↑</kbd><kbd>↓</kbd> Move</span>
          <span><kbd>Enter</kbd> Set</span>
          <span><kbd>Esc</kbd> Close</span>
        </footer>
      </div>
    </div>,
    document.body,
  );
}
