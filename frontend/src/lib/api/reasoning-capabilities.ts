// Path: frontend/src/lib/api/reasoning-capabilities.ts
//
// Single client for GET /api/config/reasoning-capabilities.
//
// This module is the ONLY place the frontend talks to the reasoning
// capabilities endpoint. Two reasons:
//
//   1. Single source of truth for the wire shape — every consumer
//      (ReasoningEffortSelector, future admin panels, debugging
//      tools) reads the SAME TypeScript interface that mirrors the
//      backend's `ReasoningCapabilityDescriptor`.
//
//   2. Provider-id resolution — the backend expects a canonical
//      provider id (e.g. "google_genai", "lmstudio_native"), but
//      `active_provider` in the UI is a display name ("Google
//      Gemini (Native)"). The resolver below prefers the canonical
//      id from models_metadata.json and falls back to the same
//      heuristic the run payload uses, so the two never disagree
//      about which provider a request targets.

/**
 * One selectable reasoning level, as returned by the backend.
 *
 * The backend is the ONLY producer of this object — it expands bare
 * level IDs ("medium", "minimal", ...) into this display shape using
 * its canonical catalog. The frontend NEVER constructs one; it only
 * renders what it receives.
 *
 * `icon` is an abstract name ("brain", "zap", "sparkles", ...). The
 * selector maps names to Lucide components. Keeping the icon as a
 * name (not a component reference) means the backend can add levels
 * without a frontend code change as long as the name exists in the
 * selector's icon map.
 */
export interface ReasoningLevelOption {
  id: string;
  label: string;
  short_label: string;
  description: string;
  badge_class: string;
  icon: string;
}

/**
 * Provider+model reasoning capability descriptor.
 *
 * Mirrors `peldrun.llm.contracts.ReasoningCapabilityDescriptor` as
 * serialized by `omweb.routers.config_rtr.get_reasoning_capabilities`.
 *
 * Render semantics (matching the plan):
 *   * supported === false        → hide the widget entirely.
 *   * mode === "none"            → hide (alias for supported=false).
 *   * mode === "automatic"       → static "Always on" badge.
 *   * mode === "toggle"          → on/off switch only (no levels).
 *   * mode === "effort"          → optional toggle + dropdown of levels.
 *   * mode === "budget"          → same UI as effort; the backend
 *                                  translates levels into a numeric
 *                                  budget downstream.
 */
export interface ReasoningCapabilityDescriptor {
  ok: boolean;
  provider_id: string;
  model_id: string;
  supported: boolean;
  mode: "none" | "toggle" | "automatic" | "effort" | "budget";
  toggle_available: boolean;
  default_level: string | null;
  options: ReasoningLevelOption[];
  /** Where the backend sourced the answer (adapter | metadata | unsupported). */
  source: string;
}

/**
 * Fetch the reasoning capability descriptor for a (provider, model)
 * pair.
 *
 * Never throws for "unknown" — the backend degrades to a well-formed
 * `supported: false` payload. Throws only on transport/HTTP errors;
 * callers should treat a thrown error the same as `supported: false`
 * (hide the widget) and log the detail.
 */
export async function fetchReasoningCapabilities(
  providerId: string,
  modelId: string,
  signal?: AbortSignal,
): Promise<ReasoningCapabilityDescriptor> {
  const params = new URLSearchParams();
  if (providerId) params.set("provider_id", providerId);
  if (modelId) params.set("model", modelId);

  const res = await fetch(
    `/api/config/reasoning-capabilities?${params.toString()}`,
    { signal },
  );
  if (!res.ok) {
    throw new Error(`HTTP ${res.status} from reasoning-capabilities`);
  }
  return (await res.json()) as ReasoningCapabilityDescriptor;
}

/**
 * Resolve the canonical provider id for the currently-active model.
 *
 * Precedence
 * ----------
 *   1. `models_metadata[model].provider_id` — the value written by
 *      Scan Models, authoritative because the backend set it from
 *      the adapter that owns the model.
 *
 *   2. Heuristic on the display name — identical to the one the run
 *      payload uses (`activeProvider.toLowerCase().replace(/[^a-z0-9]/g, "")`).
 *      This covers legacy entries written before `provider_id`
 *      tagging, and custom endpoints that never went through a scan.
 *
 * Returns an empty string when neither source yields a value; callers
 * should treat that as "unsupported / unknown" and skip the fetch.
 *
 * The reason for preferring metadata over the display name is that
 * display names are LOCALIZED and SUFFIXED ("Google Gemini (Native)",
 * "LM Studio", "Anthropic (Native)") — none of which are stable
 * identifiers the backend can match. Only metadata carries the
 * canonical value (e.g. "google_genai", "lmstudio", "anthropic").
 */
export function resolveProviderId(
  activeProvider: string | null,
  activeModel: string | null,
  metadataVault: Record<string, unknown> | null | undefined,
): string {
  // Metadata-first.
  if (activeModel && metadataVault) {
    const entry = metadataVault[activeModel];
    if (entry && typeof entry === "object") {
      const pid = (entry as { provider_id?: unknown }).provider_id;
      if (typeof pid === "string" && pid.trim()) {
        return pid.trim();
      }
    }
  }
  // Fallback: display-name heuristic (matches run payload behavior).
  return activeProvider
    ? activeProvider.toLowerCase().replace(/[^a-z0-9]/g, "")
    : "";
}
