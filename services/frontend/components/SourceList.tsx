"use client";

// "Where this came from": the desktop rail, the mobile bar + bottom sheet, and the citation
// markers in Citation.tsx all share ONE source of truth -- lib/sources.ts::buildSourceCards -- so
// the rail and the sheet can never show different cards. This module owns the shared selection
// state (which card is active, whether the mobile sheet is open) behind a React context, and the
// three rendering surfaces (SourceRail, SourceBar, SourceSheet) that read from it.

import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import type { SourceCard } from "../lib/sources";
import { withNonBreakingHyphens } from "../lib/nonbreaking";
import { parseInline } from "../lib/prose";
import { InlineNodes } from "./Message";

interface SourcesContextValue {
  cards: SourceCard[];
  activeIndex: number | null;
  select: (index: number) => void;
  sheetOpen: boolean;
  openSheet: () => void;
  closeSheet: () => void;
  railRef: RefObject<HTMLElement>;
}

const SourcesContext = createContext<SourcesContextValue | null>(null);

/** Returns the context value, or `null` when there is no provider above -- e.g. `blocked_unverified`
 * and every other view outside `answer`/`refusal_advice`, which never mount `SourcesProvider`. */
export function useSources(): SourcesContextValue | null {
  return useContext(SourcesContext);
}

export function SourcesProvider({
  cards,
  children,
}: {
  cards: SourceCard[];
  children: ReactNode;
}) {
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);
  const railRef = useRef<HTMLElement>(null);

  const select = (index: number) => {
    setActiveIndex(index);
    // Measures the REAL rail element rather than re-checking the CSS breakpoint with a JS media
    // query, so this can never drift from the `rail:` breakpoint that actually hides it: an
    // element with no client rects is not rendered as visible box (display:none via `hidden
    // rail:block`), regardless of what any duplicated width check might say.
    const railVisible = railRef.current != null && railRef.current.getClientRects().length > 0;
    if (!railVisible) setSheetOpen(true);
  };

  const openSheet = () => setSheetOpen(true);
  const closeSheet = () => setSheetOpen(false);

  useEffect(() => {
    if (!sheetOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSheetOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [sheetOpen]);

  return (
    <SourcesContext.Provider
      value={{ cards, activeIndex, select, sheetOpen, openSheet, closeSheet, railRef }}
    >
      {children}
    </SourcesContext.Provider>
  );
}

/** Renders a card's quote through the SAME inline node renderer the answer prose uses (Message.tsx's
 * `InlineNodes`), so a snippet containing `**bold**` or a markdown link renders instead of showing
 * literal asterisks. `citations={[]}` makes a bracket-shaped token inside quoted source text render
 * as inert text rather than resolving against this answer's own citation list. */
function QuoteProse({ text }: { text: string }) {
  const nodes = parseInline(withNonBreakingHyphens(text));
  return <InlineNodes nodes={nodes} citations={[]} />;
}

function SourceCardView({
  card,
  isActive,
  withDomId,
}: {
  card: SourceCard;
  isActive: boolean;
  withDomId?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isActive) return;
    const el = ref.current;
    // The rects check is what stops the OTHER (hidden) surface's copy of this same card -- the
    // sheet's copy while the rail is visible, or vice versa -- from scrolling a page it is not
    // actually shown on.
    if (el && el.getClientRects().length > 0) {
      el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [isActive]);

  return (
    <div
      id={withDomId ? `source-card-${card.index}` : undefined}
      ref={ref}
      className={`mb-3 rounded-md border bg-paper px-4 py-3.5 ${
        isActive ? "border-saffron shadow-[0_0_0_1px_var(--saffron)]" : "border-rule"
      }`}
    >
      <span className="mb-2 inline-block rounded-[3px] bg-link px-1.5 py-px text-[11px] font-semibold text-white">
        {card.index}
      </span>
      <a
        href={card.url}
        target="_blank"
        rel="noopener noreferrer"
        className="block text-sm font-medium leading-[1.4] text-link no-underline hover:underline"
      >
        {withNonBreakingHyphens(card.title)}
      </a>
      <p className="mb-[9px] mt-2.5 border-l-2 border-rule bg-sand px-[11px] py-[9px] font-serif text-[13.5px] leading-[1.65] text-body">
        {"“"}
        <QuoteProse text={card.quote} />
        {"”"}
      </p>
      <span className="block text-[11.5px] leading-[1.6] text-muted">
        {card.domain}
        {card.pageUpdated ? ` · page updated ${card.pageUpdated}` : ""}
      </span>
      {card.verified && (
        <span className="block text-[11.5px] leading-[1.6] text-muted">verified {card.verified}</span>
      )}
    </div>
  );
}

/** Sticky desktop rail, visible only above the `rail` (980px) breakpoint. Replaces the old inline
 * source list entirely on the `answer`/`refusal_advice` views. */
export function SourceRail() {
  const ctx = useSources();
  if (!ctx) return null;
  const { cards, activeIndex, railRef } = ctx;

  return (
    <aside
      ref={railRef as RefObject<HTMLElement>}
      aria-label="Where this came from"
      className="sticky top-[82px] mt-8 hidden rail:block"
    >
      <p className="mb-3 pt-0.5 text-[12.5px] font-medium tracking-[0.02em] text-muted">
        Where this came from
      </p>
      {cards.map((card) => (
        <SourceCardView key={card.index} card={card} isActive={activeIndex === card.index} withDomId />
      ))}
    </aside>
  );
}

/** Mobile bar, visible only below the `rail` breakpoint, replacing the rail's spot with a single
 * tap target that opens the bottom sheet. */
export function SourceBar() {
  const ctx = useSources();
  if (!ctx || ctx.cards.length === 0) return null;
  const n = ctx.cards.length;

  return (
    <button
      type="button"
      onClick={ctx.openSheet}
      className="mt-[26px] flex w-full items-center gap-2.5 rounded-md border border-rule bg-sand px-[15px] py-3 text-left font-sans text-sm text-ink rail:hidden"
    >
      <span className="font-medium">
        {n} source{n === 1 ? "" : "s"} cited
      </span>
      <span className="ml-auto text-[13px] text-link">Tap to inspect &rsaquo;</span>
    </button>
  );
}

/** Scrim + bottom sheet holding the same cards as the rail, for narrow viewports. */
export function SourceSheet() {
  const ctx = useSources();
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  const sheetOpen = ctx?.sheetOpen ?? false;

  // Makes `aria-modal="true"` on the dialog below actually true: on open, focus moves into the
  // sheet (to its close button); on close, focus returns to whatever had it before (typically the
  // citation marker that opened the sheet). This does NOT build a focus trap -- Tab can still move
  // focus out of the sheet and back into the page behind it while the sheet is open. A real trap is
  // a bigger change than this fix calls for, so it is deliberately not attempted here; this only
  // moves focus in and gives it back, honestly short of a full trap.
  useEffect(() => {
    if (sheetOpen) {
      previouslyFocusedRef.current =
        document.activeElement instanceof HTMLElement ? document.activeElement : null;
      closeButtonRef.current?.focus();
    } else if (previouslyFocusedRef.current) {
      previouslyFocusedRef.current.focus();
      previouslyFocusedRef.current = null;
    }
  }, [sheetOpen]);

  if (!ctx || !ctx.sheetOpen) return null;
  const { cards, activeIndex, closeSheet } = ctx;

  return (
    <>
      <div
        className="fixed inset-0 z-50 bg-[rgba(20,12,4,0.34)]"
        onClick={closeSheet}
        aria-hidden="true"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Where this came from"
        className="fixed inset-x-0 bottom-0 z-[60] max-h-[74vh] overflow-auto rounded-t-xl border-t border-rule bg-paper px-5 pb-[26px] pt-4 shadow-[0_-8px_28px_rgba(20,12,4,0.18)]"
      >
        {/* Sticky, not absolute: the sheet's cards carry DOM ids and can scroll (unlike the
            prototype's, which never scrolls), so an absolutely-positioned close button scrolls
            away with the content. This header row pins to the top of the scrolling container
            instead, opaque over whatever passes beneath it. */}
        <div className="sticky top-0 z-[1] -mx-5 -mt-4 mb-3.5 flex items-center justify-between bg-paper px-5 pb-3.5 pt-4">
          <h3 className="text-[13px] font-medium text-muted">Where this came from</h3>
          <button
            type="button"
            ref={closeButtonRef}
            onClick={closeSheet}
            aria-label="Close"
            className="text-[20px] leading-none text-muted"
          >
            {"×"}
          </button>
        </div>
        {cards.map((card) => (
          <SourceCardView key={card.index} card={card} isActive={activeIndex === card.index} />
        ))}
      </div>
    </>
  );
}
