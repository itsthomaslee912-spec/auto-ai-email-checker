import {
  Children,
  isValidElement,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type ReactElement,
  type ReactNode,
  type SelectHTMLAttributes,
} from "react";
import { createPortal } from "react-dom";

type CompactSelectProps = SelectHTMLAttributes<HTMLSelectElement> & {
  containerClassName?: string;
};

type SelectOption = {
  value: string;
  label: ReactNode;
  text: string;
  disabled: boolean;
};

function textFromNode(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  return Children.toArray(node).map(textFromNode).join("");
}

function optionsFromChildren(children: ReactNode): SelectOption[] {
  return Children.toArray(children).flatMap((child) => {
    if (!isValidElement(child)) return [];
    const element = child as ReactElement<{ value?: string | number; disabled?: boolean; children?: ReactNode }>;
    if (element.type !== "option") return optionsFromChildren(element.props.children);
    const label = element.props.children;
    return [{
      value: element.props.value == null ? textFromNode(label) : String(element.props.value),
      label,
      text: textFromNode(label),
      disabled: Boolean(element.props.disabled),
    }];
  });
}

export default function CompactSelect({
  children,
  className = "",
  containerClassName = "",
  disabled,
  id,
  value,
  defaultValue,
  onChange,
  onBlur,
  onFocus,
  required,
  name,
  form,
  "aria-invalid": ariaInvalid,
  "aria-label": ariaLabel,
  "aria-labelledby": ariaLabelledBy,
  "aria-describedby": ariaDescribedBy,
  ...props
}: CompactSelectProps) {
  const generatedId = useId().replace(/:/g, "");
  const controlId = id ?? `compact-select-${generatedId}`;
  const listboxId = `${controlId}-listbox`;
  const options = useMemo(() => optionsFromChildren(children), [children]);
  const initialValue = defaultValue == null ? options[0]?.value ?? "" : String(defaultValue);
  const [uncontrolledValue, setUncontrolledValue] = useState(initialValue);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [menuStyle, setMenuStyle] = useState<CSSProperties>({});
  const rootRef = useRef<HTMLSpanElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const selectRef = useRef<HTMLSelectElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef("");
  const searchTimerRef = useRef<number | undefined>(undefined);
  const selectedValue = value == null ? uncontrolledValue : String(value);
  const selectedIndex = Math.max(0, options.findIndex((option) => option.value === selectedValue));
  const selectedOption = options[selectedIndex];

  const updateMenuPosition = () => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const gutter = 8;
    const width = Math.min(Math.max(rect.width, 220), window.innerWidth - gutter * 2);
    const below = window.innerHeight - rect.bottom - gutter;
    const above = rect.top - gutter;
    const openAbove = below < 220 && above > below;
    const maxHeight = Math.max(120, Math.min(320, openAbove ? above : below));
    setMenuStyle({
      left: Math.min(rect.left, window.innerWidth - width - gutter),
      top: openAbove ? Math.max(gutter, rect.top - maxHeight - 6) : rect.bottom + 6,
      width,
      maxHeight,
      transformOrigin: openAbove ? "bottom" : "top",
    });
  };

  const openMenu = () => {
    if (disabled || !options.length) return;
    setActiveIndex(selectedIndex);
    updateMenuPosition();
    setOpen(true);
  };

  const closeMenu = () => setOpen(false);

  useEffect(() => {
    if (!open) return;
    updateMenuPosition();
    const reposition = () => updateMenuPosition();
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !menuRef.current?.contains(target)) closeMenu();
    };
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => {
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
    };
  }, [open]);

  useEffect(() => () => window.clearTimeout(searchTimerRef.current), []);

  const choose = (nextValue: string) => {
    if (value == null) setUncontrolledValue(nextValue);
    const select = selectRef.current;
    if (select) {
      select.value = nextValue;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }
    closeMenu();
    requestAnimationFrame(() => triggerRef.current?.focus());
  };

  const moveActive = (direction: 1 | -1) => {
    if (!options.length) return;
    let next = activeIndex;
    do next = (next + direction + options.length) % options.length;
    while (options[next]?.disabled && next !== activeIndex);
    setActiveIndex(next);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) openMenu();
      else moveActive(event.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      if (!open) openMenu();
      const indexes = options.map((_, index) => index).filter((index) => !options[index]?.disabled);
      setActiveIndex(event.key === "Home" ? indexes[0] ?? 0 : indexes.at(-1) ?? 0);
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (!open) openMenu();
      else if (!options[activeIndex]?.disabled) choose(options[activeIndex]?.value ?? "");
      return;
    }
    if (event.key === "Escape" && open) {
      event.preventDefault();
      closeMenu();
      return;
    }
    if (event.key === "Tab") {
      closeMenu();
      return;
    }
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      searchRef.current += event.key.toLocaleLowerCase();
      window.clearTimeout(searchTimerRef.current);
      searchTimerRef.current = window.setTimeout(() => { searchRef.current = ""; }, 600);
      const match = options.findIndex((option) => !option.disabled && option.text.toLocaleLowerCase().startsWith(searchRef.current));
      if (match >= 0) {
        event.preventDefault();
        if (!open) openMenu();
        setActiveIndex(match);
      }
    }
  };

  return (
    <span
      ref={rootRef}
      className={`compact-select ${containerClassName}`.trim()}
      data-disabled={disabled ? "true" : undefined}
      data-invalid={ariaInvalid === true || ariaInvalid === "true" ? "true" : undefined}
      data-open={open ? "true" : undefined}
    >
      <select
        {...props}
        ref={selectRef}
        className="compact-select-native"
        id={`${controlId}-native`}
        name={name}
        form={form}
        value={selectedValue}
        disabled={disabled}
        required={required}
        tabIndex={-1}
        aria-hidden="true"
        onChange={onChange}
      >
        {children}
      </select>
      <button
        ref={triggerRef}
        id={controlId}
        type="button"
        className={`compact-select-trigger ${className}`.trim()}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        aria-activedescendant={open ? `${listboxId}-option-${activeIndex}` : undefined}
        aria-invalid={ariaInvalid}
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledBy}
        aria-describedby={ariaDescribedBy}
        aria-required={required || undefined}
        onClick={() => open ? closeMenu() : openMenu()}
        onKeyDown={handleKeyDown}
        onFocus={(event) => onFocus?.(event as unknown as React.FocusEvent<HTMLSelectElement>)}
        onBlur={(event) => onBlur?.(event as unknown as React.FocusEvent<HTMLSelectElement>)}
      >
        <span className="compact-select-value">{selectedOption?.label ?? "Select an option"}</span>
        <span className="compact-select-indicator" aria-hidden="true">
          <svg viewBox="0 0 16 16"><path d="m4.5 6.25 3.5 3.5 3.5-3.5" /></svg>
        </span>
      </button>
      {open && createPortal(
        <div
          ref={menuRef}
          id={listboxId}
          className="compact-select-menu"
          style={menuStyle}
          role="listbox"
          aria-label={ariaLabel}
          aria-labelledby={ariaLabelledBy}
        >
          {options.map((option, index) => (
            <div
              id={`${listboxId}-option-${index}`}
              className="compact-select-option"
              data-active={index === activeIndex ? "true" : undefined}
              data-selected={option.value === selectedValue ? "true" : undefined}
              data-disabled={option.disabled ? "true" : undefined}
              role="option"
              aria-selected={option.value === selectedValue}
              aria-disabled={option.disabled || undefined}
              key={`${option.value}-${index}`}
              onPointerMove={() => !option.disabled && setActiveIndex(index)}
              onPointerDown={(event) => event.preventDefault()}
              onClick={() => !option.disabled && choose(option.value)}
            >
              <span>{option.label}</span>
              <svg className="compact-select-check" viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 8.25 3 3 6-6" /></svg>
            </div>
          ))}
        </div>,
        document.body,
      )}
    </span>
  );
}
