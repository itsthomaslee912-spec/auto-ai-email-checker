import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

const TOOLTIP_TARGETS = [
  "button",
  "a[href]",
  "input",
  "textarea",
  "select",
  "[role='button']",
  "[role='menuitem']",
  "[role='menuitemradio']",
  "[role='option']",
  "[role='radio']",
  "[role='switch']",
  "[role='tab']",
  "[data-tooltip]",
  "[title]",
].join(",");

const EMAIL_SYNC_TARGET = ".email-sync-status";
const CALENDAR_ROOT = ".calendar-workspace";
const MAX_TOOLTIP_LENGTH = 180;
const POINTER_DELAY_MS = 420;

type TooltipState = {
  target: HTMLElement;
  message: string;
  hint: string;
  full: boolean;
  placement: "below" | "right" | "top";
  left: number;
  top: number;
  visible: boolean;
};

function isFull(target: HTMLElement): boolean {
  return target.dataset.tooltipFull != null;
}

function placementFor(target: HTMLElement): TooltipState["placement"] {
  const placement = target.dataset.tooltipPlacement;
  if (placement === "right" || placement === "top") return placement;
  return "below";
}

function hintFor(target: HTMLElement): string {
  return target.dataset.tooltipHint?.replace(/\s+/g, " ").trim() || "";
}

function normalizedMessage(value: string | null | undefined): string {
  const message = value?.replace(/\s+/g, " ").trim() || "";
  if (message.length <= MAX_TOOLTIP_LENGTH) return message;
  return `${message.slice(0, MAX_TOOLTIP_LENGTH - 1).trimEnd()}…`;
}

function messageFor(target: HTMLElement): string {
  const explicit = target.dataset.tooltip;
  if (explicit && isFull(target)) {
    return explicit.split("\n").map((line) => line.trim()).filter(Boolean).join("\n");
  }
  const ariaLabel = target.getAttribute("aria-label");
  const nativeTitle = target.getAttribute("title") || target.dataset.appNativeTitle;
  const placeholder = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement
    ? target.placeholder
    : "";
  const fieldLabel = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement
    ? target.labels?.[0]?.textContent
    : "";
  const text = target instanceof HTMLInputElement && target.type !== "button" && target.type !== "submit"
    ? ""
    : target.textContent;
  return normalizedMessage(explicit || ariaLabel || nativeTitle || placeholder || fieldLabel || text);
}

function tooltipTarget(node: EventTarget | null): HTMLElement | null {
  if (!(node instanceof Element)) return null;
  if (node.closest("[role='tooltip']") || node.closest(".calendar-event")) return null;
  const sync = node.closest<HTMLElement>(EMAIL_SYNC_TARGET);
  if (sync) return messageFor(sync) ? sync : null;
  const explicit = node.closest<HTMLElement>("[data-tooltip]");
  if (explicit) return messageFor(explicit) ? explicit : null;
  const calendarRoot = node.closest(CALENDAR_ROOT);
  if (!calendarRoot) return null;
  const target = node.closest<HTMLElement>(TOOLTIP_TARGETS);
  if (!target || !calendarRoot.contains(target)) return null;
  return messageFor(target) ? target : null;
}

export default function AppTooltip() {
  const [tooltip, setTooltip] = useState<TooltipState | null>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const showTimer = useRef<number | null>(null);
  const activeTarget = useRef<HTMLElement | null>(null);

  const clearTimer = useCallback(() => {
    if (showTimer.current != null) window.clearTimeout(showTimer.current);
    showTimer.current = null;
  }, []);

  const restoreNativeTitle = useCallback((target: HTMLElement | null) => {
    if (!target?.dataset.appNativeTitle) return;
    target.setAttribute("title", target.dataset.appNativeTitle);
    delete target.dataset.appNativeTitle;
  }, []);

  const hide = useCallback((target?: HTMLElement | null) => {
    if (target && activeTarget.current !== target) return;
    clearTimer();
    restoreNativeTitle(activeTarget.current);
    activeTarget.current = null;
    setTooltip(null);
  }, [clearTimer, restoreNativeTitle]);

  const show = useCallback((target: HTMLElement, immediate: boolean) => {
    clearTimer();
    if (activeTarget.current && activeTarget.current !== target) restoreNativeTitle(activeTarget.current);
    activeTarget.current = target;
    const title = target.getAttribute("title");
    if (title) {
      target.dataset.appNativeTitle = title;
      target.removeAttribute("title");
    }
    const reveal = () => {
      if (activeTarget.current !== target || !target.isConnected) return;
      const rect = target.getBoundingClientRect();
      const placement = placementFor(target);
      setTooltip({
        target,
        message: messageFor(target),
        hint: isFull(target) ? "" : hintFor(target),
        full: isFull(target),
        placement,
        left: placement === "right" ? rect.right + 10 : placement === "top" ? rect.left : rect.left + rect.width / 2,
        top: placement === "right" || placement === "top" ? rect.top : rect.bottom + 10,
        visible: false,
      });
    };
    if (immediate) reveal();
    else showTimer.current = window.setTimeout(reveal, POINTER_DELAY_MS);
  }, [clearTimer, restoreNativeTitle]);

  useEffect(() => {
    const onPointerOver = (event: PointerEvent) => {
      if (event.pointerType === "touch") return;
      const target = tooltipTarget(event.target);
      if (!target || target === activeTarget.current) return;
      show(target, false);
    };
    const onPointerOut = (event: PointerEvent) => {
      const target = tooltipTarget(event.target);
      if (!target || target !== activeTarget.current) return;
      if (event.relatedTarget instanceof Node && target.contains(event.relatedTarget)) return;
      hide(target);
    };
    const onFocusIn = (event: FocusEvent) => {
      const target = tooltipTarget(event.target);
      if (target) show(target, true);
    };
    const onFocusOut = (event: FocusEvent) => {
      const target = tooltipTarget(event.target);
      if (target) hide(target);
    };
    const dismiss = () => hide();

    document.addEventListener("pointerover", onPointerOver, true);
    document.addEventListener("pointerout", onPointerOut, true);
    document.addEventListener("focusin", onFocusIn, true);
    document.addEventListener("focusout", onFocusOut, true);
    document.addEventListener("pointerdown", dismiss, true);
    window.addEventListener("blur", dismiss);
    window.addEventListener("resize", dismiss);
    window.addEventListener("scroll", dismiss, true);
    return () => {
      document.removeEventListener("pointerover", onPointerOver, true);
      document.removeEventListener("pointerout", onPointerOut, true);
      document.removeEventListener("focusin", onFocusIn, true);
      document.removeEventListener("focusout", onFocusOut, true);
      document.removeEventListener("pointerdown", dismiss, true);
      window.removeEventListener("blur", dismiss);
      window.removeEventListener("resize", dismiss);
      window.removeEventListener("scroll", dismiss, true);
      hide();
    };
  }, [hide, show]);

  useLayoutEffect(() => {
    if (!tooltip || !tooltipRef.current) return;
    const node = tooltipRef.current;
    const targetRect = tooltip.target.getBoundingClientRect();
    const bounds = node.getBoundingClientRect();
    const gutter = 10;
    if (tooltip.placement === "right") {
      const fitsRight = targetRect.right + gutter + bounds.width <= window.innerWidth - gutter;
      const left = fitsRight ? targetRect.right + gutter : Math.max(gutter, targetRect.left - gutter - bounds.width);
      const centered = targetRect.top + targetRect.height / 2 - bounds.height / 2;
      const top = Math.max(gutter, Math.min(window.innerHeight - gutter - bounds.height, centered));
      setTooltip((current) => current && current.target === tooltip.target
        ? { ...current, left, top, visible: true }
        : current);
      return;
    }
    if (tooltip.placement === "top") {
      const left = Math.max(gutter, Math.min(window.innerWidth - gutter - bounds.width, targetRect.left));
      const top = Math.max(gutter, targetRect.top - gutter - bounds.height);
      setTooltip((current) => current && current.target === tooltip.target
        ? { ...current, left, top, visible: true }
        : current);
      return;
    }
    const left = Math.max(gutter + bounds.width / 2, Math.min(window.innerWidth - gutter - bounds.width / 2, targetRect.left + targetRect.width / 2));
    const fitsBelow = targetRect.bottom + gutter + bounds.height <= window.innerHeight - gutter;
    const top = fitsBelow ? targetRect.bottom + gutter : targetRect.top - gutter - bounds.height;
    setTooltip((current) => current && current.target === tooltip.target
      ? { ...current, left, top: Math.max(gutter, top), visible: true }
      : current);
  }, [tooltip?.target, tooltip?.message]);

  if (!tooltip) return null;
  const [heading, ...lines] = tooltip.full ? tooltip.message.split("\n") : [tooltip.message];
  return (
    <div
      ref={tooltipRef}
      className={`app-tooltip placement-${tooltip.placement}${tooltip.full ? " is-detailed" : ""}${tooltip.hint ? " has-hint" : ""}${tooltip.visible ? " is-visible" : ""}`}
      role="tooltip"
      style={{ left: tooltip.left, top: tooltip.top }}
    >
      {tooltip.hint ? (
        <>
          <strong className="app-tooltip-title">{tooltip.message}</strong>
          <span className="app-tooltip-hint">{tooltip.hint}</span>
        </>
      ) : tooltip.full ? (
        <>
          <strong className="app-tooltip-heading">{heading}</strong>
          {lines.map((line, index) => {
            if (!line.startsWith("• ")) return <p className="app-tooltip-line" key={index}>{line}</p>;
            const [label, ...rest] = line.slice(2).split("\t");
            return (
              <div className="app-tooltip-item" key={index}>
                {rest.length ? <><strong>{label}</strong><span>{rest.join(" ")}</span></> : label}
              </div>
            );
          })}
        </>
      ) : tooltip.message}
    </div>
  );
}
