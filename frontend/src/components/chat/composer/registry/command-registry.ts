// Path: frontend/src/components/chat/composer/registry/command-registry.ts

export type CommandCategory = 'system' | 'agent' | 'tool' | 'skill' | 'custom';

export interface SlashCommandExecutionContext {
  clearEditor: () => void;
  insertText: (text: string) => void;
  dispatchCustomEvent: (name: string, detail?: unknown) => void;
}

export interface SlashCommandItem {
  id: string;
  trigger: string;
  label: string;
  description: string;
  category: CommandCategory;
  icon?: string;
  /**
   * Optional extra strings matched by the slash-menu search in addition
   * to ``label``, ``trigger``, and ``description``.
   *
   * Used by skills to expose their display name and tags so typing
   * ``/review`` matches a skill whose id is ``code_review`` but whose
   * name is ``Code Review`` and whose tags include ``review``.
   *
   * The slash menu matches case-insensitively against the whole list.
   * Order is irrelevant — inclusion is what matters.
   */
  searchTerms?: string[];
  execute: (context: SlashCommandExecutionContext) => void | Promise<void>;
}

export type MentionCategory =
  | 'agent'
  | 'tool'
  | 'skill'
  | 'file'
  | 'folder'
  | 'custom';

// [FIX-1.1] Add explicit mentionToken field for reliable matching.
// `insertText` is what gets inserted into the editor (no @ prefix).
// `mentionToken` is the bare identifier used for existence checks.
// `label` stays user-facing (with @) for display in the menu.
// `metadata` carries canonical file info (relPath, fileName, origin).
export interface MentionItem {
  id: string;
  label: string;
  description?: string;
  category: MentionCategory;
  icon?: string;
  insertText: string;
  /**
   * Optional bare identifier used for existence checks.
   * When omitted, `containsMention` falls back to `insertText` with
   * @/backtick characters stripped.
   *
   * New code (as of the mention-refactor) SHOULD always populate this.
   * Legacy call sites may omit it during the transition period.
   */
  mentionToken?: string;
  metadata?: Record<string, unknown>;
}

export interface MentionContext {
  chatId?: string;
    /**
   * Project scope for the active session. When provided, the file
   * mention provider queries the project workspace endpoint instead
   * of the per-chat directory, listing every file the agent can see.
   */
  projectId?: string | null;
  /**
   * Current browse directory within the resolved scope, relative to the
   * scope root. Empty string (or omitted) means the scope root itself.
   *
   * Populated by the typeahead plugin as the user drills into folders;
   * reset to empty every time the @ menu closes. Never persisted across
   * sessions -- folder navigation is a per-interaction concern.
   */
  currentPath?: string;
}

export type DynamicMentionProvider = (
  query: string,
  context?: MentionContext
) => Promise<MentionItem[]> | MentionItem[];

export interface ExecutionDirectives {
  agentId?: string;
  toolIds: string[];
}

/**
 * Extracts the canonical runtime execution identifier from a mention item.
 * Strictly uses source metadata identifiers rather than UI labels or descriptions.
 *
 * [FIX-1.7] Extended to support `category === 'file'`. Returns the canonical
 * relative path within the workspace, which is what the backend needs to
 * resolve and inject the file content.
 */
export function getMentionId(item: MentionItem): string | undefined {
  if (item.category === 'agent') {
    const id = item.metadata?.agentId;
    if (typeof id === 'string' && id.trim()) {
      return id.trim();
    }
    if (typeof item.id === 'string' && item.id.startsWith('agent-')) {
      return item.id.replace(/^agent-/, '');
    }
    return undefined;
  }

  if (item.category === 'tool') {
    const id = item.metadata?.toolId;
    if (typeof id === 'string' && id.trim()) {
      return id.trim();
    }
    if (typeof item.id === 'string' && item.id.startsWith('tool-')) {
      return item.id.replace(/^tool-/, '');
    }
    return undefined;
  }

  // [FIX-1.7] File mentions: return the canonical relative path.
  // Preferred source: metadata.relPath (set by the workspace files provider).
  // Fallback: strip the `file-` prefix from the auto-generated id.
  if (item.category === 'file') {
    const relPath = item.metadata?.relPath;
    if (typeof relPath === 'string' && relPath.trim()) {
      return relPath.trim();
    }
    if (typeof item.id === 'string' && item.id.startsWith('file-')) {
      return item.id.replace(/^file-/, '');
    }
    return undefined;
  }

  return undefined;
}

/**
 * Checks whether the draft prompt text currently contains the mention token.
 *
 * [FIX-1.6] Now matches THREE representations for backward compatibility:
 *   1. New bare form:      "report.md"
 *   2. Legacy @-form:      "@report.md"
 *   3. Backtick form:      "`report.md`"
 *
 * This ensures that:
 *   - Prompts produced by the new composer (no @) still resolve correctly.
 *   - Prompts copied from older chats / pasted from other apps still resolve.
 *   - Prompts where the user typed backticks manually still resolve.
 */
export function containsMention(text: string, item: MentionItem): boolean {
  const token = (item.mentionToken || item.insertText || '').trim();
  if (!token) return false;

  // Strip any leading/trailing @, `, [, or ] characters from the token.
  // Also strip a leading `@[` and trailing `]` for the bracketed form.
  const bare = token
    .replace(/^@\[|\]$/g, '')       // strip @[ ... ] wrapper if present
    .replace(/^[@`]+|[@`]+$/g, '')   // strip any residual @ or ` chars
    .trim();
  if (!bare) return false;

  const words = text.split(/\s+/);
  return (
    words.includes(bare) ||
    words.includes(`@${bare}`) ||
    words.includes(`\`${bare}\``) ||
    words.includes(`\`${bare}`) ||
    words.includes(`${bare}\``) ||
    words.includes(`@[${bare}]`)   // [FIX-5.5] bracketed marker
  );
}

/**
 * Resolves verified execution directives from active user selections still present in the text.
 * Enforces single target agent policy (latest selected) and deduplicated tools.
 *
 * NOTE: Files are NOT resolved here — they travel separately via `context_refs`
 * in the run payload. This function remains the source of truth for agent/tool
 * directives only.
 */
export function resolveExecutionDirectives(
  text: string,
  mentions: MentionItem[]
): ExecutionDirectives {
  const active = mentions.filter((item) => containsMention(text, item));

  const agents = active.filter((item) => item.category === 'agent');

  const tools = active
    .filter((item) => item.category === 'tool')
    .map((item) => getMentionId(item))
    .filter((id): id is string => Boolean(id));

  const lastAgent = agents.at(-1);

  return {
    agentId: lastAgent ? getMentionId(lastAgent) : undefined,
    toolIds: [...new Set(tools)],
  };
}

/**
 * Workspace Browser Provider (folder-aware).
 *
 * Replaces the old flat-file providers so the @ menu shows EXACTLY what
 * the workspace contains at the current drill-down level: folders first,
 * then files, like a standard file picker. The three previous providers
 * (``sessionWorkspaceFilesProvider``, ``storeAgentsProvider``,
 * ``storeToolsProvider``) are gone: agents and tools are selected from
 * the toolbar, not from the @ menu.
 *
 * Scope resolution (highest precedence first)
 * -------------------------------------------
 *   * ``context.projectId`` -> scope=project (project workspace, may be
 *     an external path that lives outside storage/).
 *   * ``context.chatId``    -> scope=chat (per-chat files directory).
 *   * neither               -> scope=global (project root).
 *
 * The global fallback keeps @ usable on the landing page before any
 * chat or project exists, which matches the pre-existing behaviour where
 * the menu always worked outside a session.
 *
 * Folder navigation
 * -----------------
 * ``context.currentPath`` selects the directory to list within the
 * scope. Empty string (or undefined) means the scope root. The typeahead
 * plugin owns this state and passes it on every ``getMentionItems``
 * call. Folders are returned as ``category: 'folder'`` items with an
 * empty ``insertText``: selecting one navigates into it rather than
 * inserting text. Files keep the pre-existing structured-mention shape
 * (``@[name]`` + ``metadata.relPath``) so the composer's file resolution
 * pipeline is unchanged.
 *
 * Why level-scoped, not recursive
 * -------------------------------
 * The old provider returned every file in the workspace as a flat list.
 * That becomes unusable once the workspace grows past a few hundred
 * entries and gives no way to reach folders that are empty at the
 * moment. Folder-by-folder navigation mirrors the screenshot UX and
 * stays readable at any workspace size. Recursive fuzzy search can be
 * layered on later as an opt-in; it is intentionally NOT part of this
 * change.
 */
async function workspaceBrowserProvider(
  query = '',
  context?: MentionContext
): Promise<MentionItem[]> {
  if (typeof window === 'undefined') return [];

  const params = new URLSearchParams();
  if (context?.projectId) {
    params.set('scope', 'project');
    params.set('project_id', context.projectId);
  } else if (context?.chatId) {
    params.set('scope', 'chat');
    params.set('chat_id', context.chatId);
  } else {
    return [];
  }

  const currentPath = (context?.currentPath || '')
    .replace(/\\/g, '/')
    .trim();
  if (currentPath) params.set('path', currentPath);
  if (query) params.set('query', query);

  try {
    const res = await fetch(`/api/files/browse?${params.toString()}`);
    if (!res.ok) {
      // Non-OK means the endpoint rejected the request (bad scope,
      // traversal attempt, project not found). A genuine error worth
      // surfacing in the console -- unlike an empty folder, which is a
      // legitimate and common state.
      console.debug(
        `[CommandRegistry] browse failed: ${res.status} ${res.statusText}`
      );
      return [];
    }

    const data = await res.json();
    const entries: Array<{
      name: string;
      path: string;
      is_dir: boolean;
      type: string;
      size: number;
    }> = Array.isArray(data?.entries) ? data.entries : [];

    return entries.map((entry) => {
      const fullPath = entry.path;

      if (entry.is_dir) {
        // Folders: no insertText. Selecting one triggers a drill-down in
        // the typeahead plugin's folder branch. The label omits any
        // trailing slash so the menu reads like an IDE sidebar; the
        // description spells out the kind explicitly.
        return {
          id: `folder-${fullPath}`,
          label: entry.name,
          description: `Folder \u00b7 ${fullPath}`,
          category: 'folder' as MentionCategory,
          insertText: '',
          mentionToken: entry.name,
          icon: 'Folder',
          metadata: {
            folderPath: fullPath,
            isDir: true,
          },
        };
      }

      return {
        id: `file-${fullPath}`,
        label: `@${entry.name}`,
        description: `Workspace \u00b7 ${fullPath}`,
        category: 'file' as MentionCategory,
        insertText: `@[${entry.name}] `,
        mentionToken: entry.name,
        icon: 'FileText',
        metadata: {
          relPath: fullPath,
          fileName: entry.name,
          origin: 'workspace',
        },
      };
    });
  } catch (err) {
    console.error('[CommandRegistry] browse provider error:', err);
    return [];
  }
}





class CommandRegistryStore {
  private slashCommands: Map<string, SlashCommandItem> = new Map();
  private staticMentions: Map<string, MentionItem> = new Map();
  private dynamicProviders: Set<DynamicMentionProvider> = new Set();
  private listeners: Set<() => void> = new Set();

  constructor() {
    this.registerDefaultCommands();
    // Only one dynamic mention provider remains: the workspace browser.
    // Agents and tools are chosen from the composer toolbar (AgentSelector,
    // ActiveToolsModal) -- not from the @ menu. Re-adding them here would
    // reintroduce the clutter this provider consolidation removed.
    this.registerMentionProvider(workspaceBrowserProvider);
    this.setupExternalEventListener();
  }

  private setupExternalEventListener(): void {
    if (typeof window === 'undefined') return;

    window.addEventListener('peldrun:register-slash-command', ((
      event: CustomEvent<SlashCommandItem>
    ) => {
      if (event.detail && event.detail.id) {
        this.registerSlashCommand(event.detail);
      }
    }) as EventListener);

    window.addEventListener('peldrun:register-mention-item', ((
      event: CustomEvent<MentionItem>
    ) => {
      if (event.detail && event.detail.id) {
        this.registerMentionItem(event.detail);
      }
    }) as EventListener);

    window.addEventListener('peldrun:register-mention-provider', ((
      event: CustomEvent<{ provider: DynamicMentionProvider }>
    ) => {
      if (event.detail && typeof event.detail.provider === 'function') {
        this.registerMentionProvider(event.detail.provider);
      }
    }) as EventListener);
  }

  private registerDefaultCommands(): void {
    this.registerSlashCommand({
      id: 'cmd-clear',
      trigger: 'clear',
      label: '/clear',
      description: 'Reset conversation session and clear editor',
      category: 'system',
      icon: 'Trash2',
      execute: ({ clearEditor, dispatchCustomEvent }) => {
        clearEditor();
        dispatchCustomEvent('peldrun:new-session');
      },
    });

    this.registerSlashCommand({
      id: 'cmd-agent-mode',
      trigger: 'agent',
      label: '/agent',
      description: 'Switch execution mode to Autonomous Agent',
      category: 'system',
      icon: 'Bot',
      execute: ({ clearEditor, dispatchCustomEvent }) => {
        clearEditor();
        dispatchCustomEvent('omweb:mode-change', 'agent');
      },
    });

    this.registerSlashCommand({
      id: 'cmd-chat-mode',
      trigger: 'chat',
      label: '/chat',
      description: 'Switch execution mode to Direct Chat',
      category: 'system',
      icon: 'MessageSquare',
      execute: ({ clearEditor, dispatchCustomEvent }) => {
        clearEditor();
        dispatchCustomEvent('omweb:mode-change', 'chat');
      },
    });
  }

  public registerSlashCommand(command: SlashCommandItem): () => void {
    this.slashCommands.set(command.id, command);
    this.notify();
    return () => {
      this.slashCommands.delete(command.id);
      this.notify();
    };
  }

  public registerMentionItem(mention: MentionItem): () => void {
    this.staticMentions.set(mention.id, mention);
    this.notify();
    return () => {
      this.staticMentions.delete(mention.id);
      this.notify();
    };
  }

  public registerMentionProvider(provider: DynamicMentionProvider): () => void {
    this.dynamicProviders.add(provider);
    this.notify();
    return () => {
      this.dynamicProviders.delete(provider);
      this.notify();
    };
  }

  public getSlashCommands(query = ''): SlashCommandItem[] {
    const list = Array.from(this.slashCommands.values());
    if (!query) return list;
    const lower = query.toLowerCase();

    // Rank results so the most specific match wins:
    //   1. Label starts with the query (e.g. "/code" matches "/code_review").
    //   2. Any searchTerm starts with the query.
    //   3. Trigger / label / description / searchTerm contains the query.
    //
    // The sort is stable within each tier, preserving registration
    // order (system commands first, then skills, then any custom
    // additions). This keeps /clear, /agent, /chat pinned at the top
    // when the query is empty but lets skills bubble up when the user
    // types a prefix that only they match.
    // [P7.1b] Hyphen/underscore normalization. Users frequently type
    // the repository form (`/code-review`) even though the skill ID is
    // canonicalized to `code_review`. Normalizing both sides of the
    // match keeps every spelling working without touching the
    // registered ID.
    const normalized = lower.replace(/-/g, '_');

    const scored: Array<{ item: SlashCommandItem; score: number }> = [];

    for (const c of list) {
      const labelLower = c.label.toLowerCase().replace(/-/g, '_');
      const triggerLower = c.trigger.toLowerCase().replace(/-/g, '_');
      const descriptionLower = (c.description || '').toLowerCase();
      const searchTermsLower = (c.searchTerms || []).map((t) =>
        t.toLowerCase().replace(/-/g, '_')
      );

      let score = 0;

      if (
        labelLower.startsWith(normalized) ||
        triggerLower.startsWith(normalized)
      ) {
        score += 100;
      } else if (searchTermsLower.some((t) => t.startsWith(normalized))) {
        score += 50;
      }

      if (score === 0) {
        const matchesAnywhere =
          labelLower.includes(normalized) ||
          triggerLower.includes(normalized) ||
          descriptionLower.includes(lower) ||
          searchTermsLower.some((t) => t.includes(normalized));
        if (!matchesAnywhere) continue;
        score = 10;
      }

      scored.push({ item: c, score });
    }

    scored.sort((a, b) => b.score - a.score);
    return scored.map((s) => s.item);
  }

  public async getMentionItems(
    query = '',
    context?: MentionContext
  ): Promise<MentionItem[]> {
    if (!context?.projectId && !context?.chatId) {
      return [];
    }

    const staticList = Array.from(this.staticMentions.values());
    const lower = query.toLowerCase();

    const filteredStatic = query
      ? staticList.filter(
          (m) =>
            m.label.toLowerCase().includes(lower) ||
            m.id.toLowerCase().includes(lower) ||
            (m.description && m.description.toLowerCase().includes(lower))
        )
      : staticList;

    const dynamicPromises = Array.from(this.dynamicProviders).map(
      async (provider) => {
        try {
          return await provider(query, context);
        } catch (err) {
          console.debug(
            '[CommandRegistry] Dynamic mention provider exception:',
            err
          );
          return [];
        }
      }
    );

    const dynamicResults = await Promise.all(dynamicPromises);
    const combined = [...filteredStatic, ...dynamicResults.flat()];

    const uniqueMap = new Map<string, MentionItem>();

    for (const item of combined) {
      if (item.category !== 'file' && item.category !== 'folder') {
        continue;
      }

      uniqueMap.set(item.id, item);
    }

    return Array.from(uniqueMap.values());
  }

  public subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private notify(): void {
    this.listeners.forEach((listener) => listener());
  }
}

export const commandRegistry = new CommandRegistryStore();
