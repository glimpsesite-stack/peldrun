// Path: frontend/src/components/chat/reasoning-effort-selector.tsx
"use client";

import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Brain,
  ChevronDown,
  Check,
  Zap,
  Sparkles,
  Cpu,
  Gauge,
  Flame,
  Wand2,
  Loader2,
  AlertCircle,
  BrainCog,
} from "lucide-react";
import { useAppStorage } from "@/hooks/use-app-storage";
import {
  fetchReasoningCapabilities,
  type ReasoningCapabilityDescriptor,
  type ReasoningLevelOption,
} from "@/lib/api/reasoning-capabilities";

// ---------------------------------------------------------------------------
// Icon map
// ---------------------------------------------------------------------------
//
// Backend returns abstract icon NAMES; we project each name onto a
// Lucide component here. Keeping the map frontend-side means the
// backend can add a level with `icon: "wand"` without touching the
// frontend. Unknown names fall back to `Brain` so a typo never breaks
// rendering.
const ICON_BY_NAME: Record<string, React.ElementType> = {
  zap: Zap,
  gauge: Gauge,
  sparkles: Sparkles,
  brain: Brain,
  cpu: Cpu,
  flame: Flame,
  wand: Wand2,
};

function iconFor(name: string | undefined): React.ElementType {
  return (name && ICON_BY_NAME[name]) || Brain;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------
//
// The selector no longer assumes ANYTHING about provider or model
// capabilities on its own. Both are provided by the parent — which
// already maintains `activeModel` / `activeProvider` /
// `metadataVault` for other purposes (payload construction, display
// names, etc.), so passing them here is free.
//
// `providerId` may be empty when the parent cannot resolve a canonical
// id. In that case the selector renders nothing (P5: uncertainty is
// hidden, never fabricated).
export interface ReasoningEffortSelectorProps {
  providerId: string | null;
  modelId: string | null;
  disabled?: boolean;
}

// ---------------------------------------------------------------------------
// Selector
// ---------------------------------------------------------------------------

export function ReasoningEffortSelector({
  providerId,
  modelId,
  disabled = false,
}: ReasoningEffortSelectorProps) {
  const [reasoningEffort, setReasoningEffort] = useAppStorage(
    "reasoning_effort",
  );

  const [descriptor, setDescriptor] =
    useState<ReasoningCapabilityDescriptor | null>(null);
  const [loading, setLoading] = useState(false);
  const [isOpen, setIsOpen] = useState(false);

  const dropdownRef = useRef<HTMLDivElement>(null);

  // ---------------------------------------------------------------------
  // Fetch capabilities whenever (provider, model) changes.
  //
  // The AbortController prevents a race when the user rapidly cycles
  // through models: the previous fetch is cancelled the moment a new
  // one starts, so a stale response can never overwrite the current
  // descriptor.
  // ---------------------------------------------------------------------
  useEffect(() => {
    if (!providerId || !modelId) {
      setDescriptor(null);
      setLoading(false);
      return;
    }

    const controller = new AbortController();
    setLoading(true);

    fetchReasoningCapabilities(providerId, modelId, controller.signal)
      .then((desc) => {
        if (controller.signal.aborted) return;
        setDescriptor(desc);
        setLoading(false);
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        // Transport error → treat as "unsupported" to hide the
        // widget. A broken backend must not block the composer.
        console.debug(
          "[ReasoningEffortSelector] capability fetch failed:",
          err,
        );
        setDescriptor(null);
        setLoading(false);
      });

    return () => controller.abort();
  }, [providerId, modelId]);

  // ---------------------------------------------------------------------
  // Auto-correct the stored level when the descriptor changes.
  //
  // A level valid for the previous model may not exist for the new
  // one (e.g. user had "medium" for gpt-5, switches to o1 which has
  // no "none"; or switches to gpt-4o which has NO reasoning at all).
  // In both cases the stored value would silently cause a bad API
  // call. We reset it here — once per descriptor change — using the
  // descriptor's own `default_level` when the current value is not
  // valid.
  //
  // We skip the correction when:
  //   * the descriptor is still loading,
  //   * the model has no reasoning support at all (leaving the stored
  //     value alone keeps the user's last choice for when they switch
  //     back to a reasoning model).
  // ---------------------------------------------------------------------
  useEffect(() => {
    if (!descriptor || !descriptor.supported) return;
    if (descriptor.options.length === 0) return;

    const validIds = new Set(descriptor.options.map((o) => o.id));
    if (validIds.has(reasoningEffort)) return;

    // Prefer `default_level` when it is itself valid, else the first
    // option. Never write `undefined`.
    const fallback =
      descriptor.default_level && validIds.has(descriptor.default_level)
        ? descriptor.default_level
        : descriptor.options[0]?.id;

    if (fallback && fallback !== reasoningEffort) {
      setReasoningEffort(fallback);
    }
  }, [descriptor, reasoningEffort, setReasoningEffort]);

  // ---------------------------------------------------------------------
  // Click-outside dismissal for the dropdown.
  // ---------------------------------------------------------------------
  useEffect(() => {
    if (!isOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (
        dropdownRef.current &&
        !dropdownRef.current.contains(event.target as Node)
      ) {
        setIsOpen(false);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [isOpen]);

  // ---------------------------------------------------------------------
  // Derived state (never stored — always computed from descriptor).
  // ---------------------------------------------------------------------
  const options = descriptor?.options ?? [];
  const validIds = useMemo(() => new Set(options.map((o) => o.id)), [options]);

  const currentOption: ReasoningLevelOption | null = useMemo(() => {
    return options.find((o) => o.id === reasoningEffort) ?? null;
  }, [options, reasoningEffort]);

  const isOff = reasoningEffort === "none";

  // ---------------------------------------------------------------------
  // Setters
  // ---------------------------------------------------------------------
  const handleSelect = useCallback(
    (level: string) => {
      setReasoningEffort(level);
      setIsOpen(false);
      // Notify any listener that cares (matches the historical
      // contract; kept for backward compatibility with any consumer
      // that observed this event).
      if (typeof window !== "undefined") {
        window.dispatchEvent(
          new CustomEvent("peldrun:reasoning-effort-change", {
            detail: level,
          }),
        );
      }
    },
    [setReasoningEffort],
  );

  const handleToggle = useCallback(() => {
    if (!descriptor) return;

    if (isOff) {
      // Turn ON → use the descriptor's default level (guaranteed
      // valid for mode="effort" by the backend's descriptor
      // validation), or "medium" for pure-toggle mode where there is
      // no default_level.
      const onLevel =
        descriptor.default_level &&
        (descriptor.mode === "toggle" || validIds.has(descriptor.default_level))
          ? descriptor.default_level
          : "medium";
      handleSelect(onLevel);
    } else {
      // Turn OFF → canonical "none". Safe for any mode because:
      //   * mode="effort" with toggle_available implies "none" is in
      //     the option list (the backend's validation guarantees this
      //     via the toggle_available requirement).
      //   * mode="toggle" has no levels, and the backend compiler
      //     translates "none" back to `think: false` on the wire.
      handleSelect("none");
    }
  }, [descriptor, isOff, validIds, handleSelect]);

  // ---------------------------------------------------------------------
  // Render gates (P5: never fabricate support).
  // ---------------------------------------------------------------------

  // Still loading → no flicker, render nothing.
  if (loading) return null;

  // No descriptor / not supported / mode="none" → hide entirely.
  if (!descriptor || !descriptor.supported || descriptor.mode === "none") {
    return null;
  }

  // ---------------------------------------------------------------------
  // Mode: "automatic" — always-on, no user control.
  // ---------------------------------------------------------------------
  if (descriptor.mode === "automatic") {
    return (
      <div
        className="inline-flex items-center gap-1.5 px-2 py-1 rounded-lg border text-[11px] font-medium text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 border-emerald-500/20"
        title="The provider always applies reasoning for this model. No user control is available."
      >
        <Brain size={12} className="shrink-0" />
        <span className="font-semibold">Always on</span>
      </div>
    );
  }

  // ---------------------------------------------------------------------
  // Mode: "toggle" — pure on/off, no level list.
  // ---------------------------------------------------------------------
  if (descriptor.mode === "toggle") {
    return (
      <button
        type="button"
        disabled={disabled}
        onClick={handleToggle}
        className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-lg border text-[11px] font-medium transition-all cursor-pointer ${
          isOff
            ? "text-zinc-600 dark:text-zinc-300 bg-zinc-500/10 border-zinc-500/20"
            : "text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 border-emerald-500/20"
        } ${
          disabled
            ? "opacity-50 cursor-not-allowed"
            : "hover:opacity-90 hover:scale-[1.02] active:scale-[0.98]"
        }`}
        title={
          isOff
            ? "Reasoning: OFF — click to enable"
            : "Reasoning: ON — click to disable"
        }
      >
        <BrainCog size={12} className="shrink-0" />
        <span className="font-semibold">{isOff ? "Off" : "On"}</span>
      </button>
    );
  }

  // ---------------------------------------------------------------------
  // Mode: "effort" | "budget" — the rich dropdown + optional toggle.
  // ---------------------------------------------------------------------
  const CurrentIcon = currentOption
    ? iconFor(currentOption.icon)
    : Brain;

  const currentBadgeClass =
    currentOption?.badge_class ??
    "text-zinc-600 dark:text-zinc-300 bg-zinc-500/10 border-zinc-500/20";

  return (
    <div className="relative inline-block font-sans select-none" ref={dropdownRef}>
      <div className="inline-flex items-center gap-1">
        {/* Optional toggle switch — present only when the descriptor
            declares it (i.e. "none" is a valid level). */}
        {descriptor.toggle_available && (
          <button
            type="button"
            disabled={disabled}
            onClick={handleToggle}
            className={`inline-flex items-center justify-center text-[11px] transition-all cursor-pointer ${
              isOff
                ? "text-zinc-500 dark:text-zinc-400 bg-zinc-500/10 border-zinc-500/20"
                : "text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 border-emerald-500/20"
            } ${
              disabled
                ? "opacity-50 cursor-not-allowed"
                : "hover:opacity-90 active:scale-[0.95]"
            }`}
            title={
              isOff
                ? "Reasoning: OFF — click to enable"
                : "Reasoning: ON — click to disable"
            }
          >
            <BrainCog size={15} />
          </button>
        )}

        {/* Level picker */}
        <button
          type="button"
          disabled={disabled}
          onClick={() => setIsOpen((prev) => !prev)}
          className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md border text-[11px] font-medium transition-all cursor-pointer ${currentBadgeClass} ${
            disabled
              ? "opacity-50 cursor-not-allowed"
              : "hover:opacity-90 hover:scale-[1.02] active:scale-[0.98]"
          }`}
          title={
            currentOption
              ? `Reasoning: ${currentOption.label}`
              : "Select a reasoning level"
          }
        >
          <CurrentIcon size={12} className="shrink-0" />
          <span className="font-semibold">
            {currentOption?.short_label ?? "Select"}
          </span>
          <ChevronDown
            size={11}
            className={`transition-transform duration-150 ${
              isOpen ? "rotate-180" : ""
            }`}
          />
        </button>
      </div>

      {isOpen && (
        <div className="absolute bottom-full mb-1.5 left-0 w-64 rounded-xl border border-border bg-card/95 backdrop-blur-md shadow-xl p-1 z-50 animate-in fade-in zoom-in-95 duration-150">
          <div className="px-2 py-1 text-[10px] uppercase tracking-wider font-semibold text-muted-foreground border-b border-border/60 mb-1 flex items-center justify-between">
            <span>Reasoning Effort</span>
            <Brain size={11} />
          </div>

          <div className="space-y-0.5">
            {options.map((option) => {
              const OptIcon = iconFor(option.icon);
              const isSelected = option.id === reasoningEffort;

              return (
                <button
                  key={option.id}
                  type="button"
                  onClick={() => handleSelect(option.id)}
                  className={`w-full flex items-start gap-2.5 p-2 rounded-lg text-left transition-colors cursor-pointer ${
                    isSelected
                      ? "bg-muted text-foreground font-medium"
                      : "text-muted-foreground hover:bg-muted/50 hover:text-foreground"
                  }`}
                >
                  <div
                    className={`mt-0.5 p-1 rounded-md border shrink-0 ${option.badge_class}`}
                  >
                    <OptIcon size={12} />
                  </div>

                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between text-xs leading-none">
                      <span className="font-semibold text-foreground">
                        {option.label}
                      </span>
                      {isSelected && (
                        <Check
                          size={12}
                          className="text-primary shrink-0"
                        />
                      )}
                    </div>
                    <p className="text-[10px] text-muted-foreground mt-1 leading-snug line-clamp-2">
                      {option.description}
                    </p>
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

export default ReasoningEffortSelector;
