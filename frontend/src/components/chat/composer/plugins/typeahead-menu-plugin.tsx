// frontend/src/components/chat/composer/plugins/typeahead-menu-plugin.tsx

'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext';
import {
  $createParagraphNode,
  $createTextNode,
  $getRoot,
  $getSelection,
  $isElementNode,
  $isRangeSelection,
  $isTextNode,
  COMMAND_PRIORITY_CRITICAL,
  KEY_ARROW_DOWN_COMMAND,
  KEY_ARROW_UP_COMMAND,
  KEY_ENTER_COMMAND,
  KEY_ESCAPE_COMMAND,
  KEY_TAB_COMMAND,
  LexicalEditor,
} from 'lexical';
import {
  Bot,
  ChevronLeft,
  ChevronRight,
  FileText,
  Folder,
  Sparkles,
  Terminal,
  Wrench,
} from 'lucide-react';
import {
  commandRegistry,
  MentionItem,
  SlashCommandItem,
} from '../registry/command-registry';
import {
  $createFileMentionNode,
  getDisplayPathFromRelPath,
} from '../nodes/file-mention-node';

interface TypeaheadMenuPluginProps {
  onClearEditor?: () => void;
  chatId?: string;
  /**
   * Project scope for the active session, if any. Forwarded to
   * ``commandRegistry.getMentionItems`` as part of the mention context
   * so the file browser queries the project workspace endpoint instead
   * of the per-chat or global one. Without this prop, `@` inside a
   * project chat would silently fall back to the wrong scope.
   */
  projectId?: string | null;
  /**
   * Controls the slash-command (`/`) menu. Kept separate from file
   * mentions so Chat mode can still browse folders and files with `@`
   * while slash commands remain Agent-only.
   * Defaults to `true`.
   */
  enabled?: boolean;
  /**
   * Controls the file/folder browser (`@`). It is enabled in both Chat
   * and Agent modes by default and never exposes agent, tool, or skill
   * mentions.
   */
  mentionsEnabled?: boolean;
}

function getTextBeforeCaret(editor: LexicalEditor): string {
  let textBeforeCaret = '';
  editor.getEditorState().read(() => {
    const selection = $getSelection();
    if (!$isRangeSelection(selection) || !selection.isCollapsed()) {
      return;
    }
    const anchor = selection.anchor;
    const anchorNode = anchor.getNode();

    if ($isTextNode(anchorNode)) {
      textBeforeCaret = anchorNode.getTextContent().slice(0, anchor.offset);
    } else if ($isElementNode(anchorNode)) {
      const children = anchorNode.getChildren();
      let accumulated = '';
      const childIndex = anchor.offset;
      for (let i = 0; i < childIndex && i < children.length; i++) {
        accumulated += children[i].getTextContent();
      }
      textBeforeCaret = accumulated;
    }
  });
  return textBeforeCaret;
}

export function TypeaheadMenuPlugin({
  onClearEditor,
  chatId,
  projectId,
  enabled = true,
  mentionsEnabled = true,
}: TypeaheadMenuPluginProps) {
  const [editor] = useLexicalComposerContext();
  const [isOpen, setIsOpen] = useState(false);
  const [triggerType, setTriggerType] = useState<'slash' | 'mention' | null>(
    null
  );
  const [queryString, setQueryString] = useState('');
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [dynamicMentions, setDynamicMentions] = useState<MentionItem[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [, setRegistryTick] = useState(0);
  const menuRef = useRef<HTMLDivElement | null>(null);

  /**
   * Current drill-down directory within the active scope. Empty string
   * means "the scope root" (workspace/ for chat, project workspace for
   * project scope, PROJECT_ROOT for global scope).
   *
   * This is local UI state owned by the menu, never persisted. It is
   * populated when the user selects a folder entry and reset to '' on
   * every menu close so the next `@` always starts at the scope root.
   */
  const [currentPath, setCurrentPath] = useState('');

  /**
   * Tracks the last observed trigger so we can distinguish a FRESH `@`
   * (user just typed it, drilling should start at root) from a
   * CONTINUATION (user is typing within an already-open mention menu,
   * currentPath must be preserved).
   *
   * Without this, typing `@` would reset currentPath on every keystroke
   * after a folder drill-down, making folder navigation impossible.
   */
  const lastTriggerRef = useRef<'slash' | 'mention' | null>(null);

  useEffect(() => {
    return commandRegistry.subscribe(() => {
      setRegistryTick((t) => t + 1);
    });
  }, []);

  const slashResults = useMemo(() => {
    if (triggerType !== 'slash') return [];
    return commandRegistry.getSlashCommands(queryString);
  }, [triggerType, queryString]);

  useEffect(() => {
    let active = true;

    if (triggerType === 'mention' && mentionsEnabled) {
      setIsLoading(true);

      commandRegistry
        .getMentionItems(queryString, { chatId, projectId, currentPath })
        .then((items) => {
          if (active) {
            setDynamicMentions(
              items.filter(
                (item) =>
                  item.category === 'folder' || item.category === 'file'
              )
            );
            setIsLoading(false);
          }
        })
        .catch((error) => {
          if (active) {
            console.error(
              '[TypeaheadMenuPlugin] Failed to load files/folders:',
              error
            );
            setDynamicMentions([]);
            setIsLoading(false);
          }
        });
    } else {
      setDynamicMentions([]);
      setIsLoading(false);
    }

    return () => {
      active = false;
    };
  }, [
    triggerType,
    queryString,
    chatId,
    projectId,
    currentPath,
    mentionsEnabled,
  ]);

  /**
   * Reset the drill-down whenever the menu closes, no matter the reason
   * (Escape, file selection, no more matches, disabled prop). This
   * guarantees that the next `@` always opens at the scope root — the
   * user never has to remember "where was I last time".
   */
  useEffect(() => {
    if (!isOpen) {
      setCurrentPath('');
    }
  }, [isOpen]);

  const activeResultsCount =
    triggerType === 'slash' ? slashResults.length : dynamicMentions.length;

  useEffect(() => {
    // Slash commands and file mentions have independent gates. This
    // lets the composer keep `/` Agent-only without disabling `@` in
    // direct Chat mode.
    if (!enabled && !mentionsEnabled) {
      setIsOpen(false);
      setTriggerType(null);
      return;
    }

    return editor.registerUpdateListener(() => {
      const textBeforeCaret = getTextBeforeCaret(editor);

      const slashMatch = enabled
        ? textBeforeCaret.match(/(?:^|\s)\/([a-zA-Z0-9_-]*)$/)
        : null;
      if (slashMatch) {
        // Slash commands have no drill-down; reset lastTriggerRef so a
        // subsequent @ is treated as a fresh open.
        lastTriggerRef.current = 'slash';
        setTriggerType('slash');
        setQueryString(slashMatch[1]);
        setIsOpen(true);
        setSelectedIndex(0);
        return;
      }

      const mentionMatch = mentionsEnabled
        ? textBeforeCaret.match(/(?:^|\s)@([a-zA-Z0-9_./-]*)$/)
        : null;
      if (mentionMatch) {
        // Fresh `@` (transition from null or slash) resets drill-down.
        // Continuation (user typing inside an already-open mention)
        // preserves the current folder so navigation is not reset on
        // every keystroke.
        if (lastTriggerRef.current !== 'mention') {
          setCurrentPath('');
        }
        lastTriggerRef.current = 'mention';
        setTriggerType('mention');
        setQueryString(mentionMatch[1]);
        setIsOpen(true);
        setSelectedIndex(0);
        return;
      }

      lastTriggerRef.current = null;
      setIsOpen(false);
      setTriggerType(null);
    });
  }, [editor, enabled, mentionsEnabled]);

  // Close the active menu immediately when its feature is disabled.
  useEffect(() => {
    if (!enabled && triggerType === 'slash') {
      setIsOpen(false);
      setTriggerType(null);
      lastTriggerRef.current = null;
    }

    if (!mentionsEnabled && triggerType === 'mention') {
      setIsOpen(false);
      setTriggerType(null);
      setDynamicMentions([]);
      setCurrentPath('');
      lastTriggerRef.current = null;
    }
  }, [enabled, mentionsEnabled, triggerType]);

  const handleExecuteSelection = useCallback(
    (targetIndex?: number) => {
      if (!isOpen || activeResultsCount === 0) return false;
      const indexToExecute =
        typeof targetIndex === 'number' ? targetIndex : selectedIndex;

      if (triggerType === 'slash') {
        const selectedItem: SlashCommandItem | undefined =
          slashResults[indexToExecute];
        if (!selectedItem) return false;

        editor.update(() => {
          const selection = $getSelection();
          if ($isRangeSelection(selection)) {
            const anchorNode = selection.anchor.getNode();
            if ($isTextNode(anchorNode)) {
              const text = anchorNode.getTextContent();
              const pattern = new RegExp(`\\/${queryString}$`);
              const newText = text.replace(pattern, '');
              anchorNode.setTextContent(newText);
              if (newText.trim() === '') {
                const root = $getRoot();
                root.clear();
                const p = $createParagraphNode();
                root.append(p);
                p.select(0, 0);
              } else {
                anchorNode.select();
              }
            }
          }
        });

        selectedItem.execute({
          clearEditor: () => {
            if (onClearEditor) {
              onClearEditor();
            } else {
              editor.update(() => {
                const root = $getRoot();
                root.clear();
                root.append($createParagraphNode());
              });
            }
          },
          insertText: (textToInsert: string) => {
            editor.update(() => {
              const selection = $getSelection();
              if ($isRangeSelection(selection)) {
                selection.insertText(textToInsert);
              }
            });
          },
          dispatchCustomEvent: (name, detail) => {
            if (typeof window !== 'undefined') {
              window.dispatchEvent(new CustomEvent(name, { detail }));
            }
          },
        });

        setIsOpen(false);
        return true;
      }

      if (triggerType === 'mention') {
        const selectedItem: MentionItem | undefined =
          dynamicMentions[indexToExecute];
        if (!selectedItem) return false;
        // Defensive allowlist: this menu must never execute an agent, tool,
        // skill, or custom mention even if a provider bypasses the fetch filter.
        if (selectedItem.category !== 'folder' && selectedItem.category !== 'file') {
          return false;
        }

        // ── Folder drill-down ─────────────────────────────────────
        // Selecting a folder does NOT insert anything. Instead it
        // navigates into the folder (updates currentPath) and clears
        // the typed query from the editor, leaving a bare `@` behind
        // so the user can immediately type a fresh filter within the
        // new folder. The menu stays open — this is a navigation
        // action, not a selection.
        if (selectedItem.category === 'folder') {
          const folderPath = selectedItem.metadata?.folderPath;
          if (typeof folderPath !== 'string') return false;

          let drilled = false;
          editor.update(() => {
            const selection = $getSelection();
            if (!$isRangeSelection(selection)) return;

            const anchorNode = selection.anchor.getNode();
            if (!$isTextNode(anchorNode)) return;

            const fullText = anchorNode.getTextContent();
            const cursorOffset = selection.anchor.offset;
            const textBeforeCursor = fullText.slice(0, cursorOffset);
            const textAfterCursor = fullText.slice(cursorOffset);

            // Match the `@<query>` immediately before the cursor. The
            // queryString character set is [a-zA-Z0-9_./-], so no
            // regex-special characters are possible here — the only
            // metachar within that set is `.`, which matches its
            // literal self anyway when the query is exactly what is
            // in the editor.
            const escapedQuery = queryString.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
            const pattern = new RegExp(`@${escapedQuery}$`);
            const match = textBeforeCursor.match(pattern);
            if (!match || match.index === undefined) return;

            const beforeMention = textBeforeCursor.slice(0, match.index);
            // Leave the bare `@` behind, preserving anything after the
            // cursor (user can click mid-text, drill, and continue).
            const newText = beforeMention + '@' + textAfterCursor;
            anchorNode.setTextContent(newText);
            anchorNode.select(
              beforeMention.length + 1,
              beforeMention.length + 1,
            );
            drilled = true;
          });

          if (drilled) {
            setCurrentPath(folderPath);
            setSelectedIndex(0);
          }
          // Menu remains open; the fetch effect re-runs because
          // currentPath is in its dependency array.
          return true;
        }

        // File references become structured Lexical nodes so they can be
        // styled distinctly, clicked, and carry their canonical path as a
        // node attribute. The allowlist above prevents agent/tool/skill
        // mentions from ever reaching this selection branch.
        const isFileMention =
          selectedItem.category === 'file' &&
          typeof selectedItem.metadata?.relPath === 'string' &&
          String(selectedItem.metadata.relPath).trim().length > 0;

        if (isFileMention) {
          const relPath = String(selectedItem.metadata!.relPath);
          const displayPath = getDisplayPathFromRelPath(relPath);
          const mentionText = `@[${displayPath}]`;
          const fileOrigin =
            selectedItem.metadata?.origin === 'global' ? 'global' : 'workspace';

          editor.update(() => {
            const selection = $getSelection();
            if (!$isRangeSelection(selection)) return;

            const anchorNode = selection.anchor.getNode();
            if (!$isTextNode(anchorNode)) return;

            // Split the anchor text at the cursor so we can preserve any
            // content that lives to the right of the caret. This matters
            // when the user types `@` in the middle of an existing line
            // (e.g. "file: @ to:") rather than at the end.
            const fullText = anchorNode.getTextContent();
            const cursorOffset = selection.anchor.offset;
            const textBeforeCursor = fullText.slice(0, cursorOffset);
            const textAfterCursor = fullText.slice(cursorOffset);

            const escapedQuery = queryString.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
            const pattern = new RegExp(`@${escapedQuery}$`);
            const match = textBeforeCursor.match(pattern);
            if (!match || match.index === undefined) return;

            const beforeMention = textBeforeCursor.slice(0, match.index);

            const mentionNode = $createFileMentionNode(mentionText, relPath, fileOrigin);
            const spaceNode = $createTextNode(' ');

            if (textAfterCursor.length > 0) {
              // Preserve trailing text by inserting a fresh TextNode after
              // the mention's trailing space.
              const afterNode = $createTextNode(textAfterCursor);

              if (beforeMention.length > 0) {
                anchorNode.setTextContent(beforeMention);
                anchorNode.insertAfter(mentionNode);
                mentionNode.insertAfter(spaceNode);
                spaceNode.insertAfter(afterNode);
              } else {
                anchorNode.replace(mentionNode);
                mentionNode.insertAfter(spaceNode);
                spaceNode.insertAfter(afterNode);
              }
            } else {
              // Common case: mention inserted at the end of the node.
              if (beforeMention.length > 0) {
                anchorNode.setTextContent(beforeMention);
                anchorNode.insertAfter(mentionNode);
                mentionNode.insertAfter(spaceNode);
              } else {
                anchorNode.replace(mentionNode);
                mentionNode.insertAfter(spaceNode);
              }
            }

            // Place the cursor immediately after the mention's trailing
            // space, before any preserved trailing text.
            spaceNode.select(1, 1);
          });

          if (typeof window !== 'undefined') {
            window.dispatchEvent(
              new CustomEvent('peldrun:mention-selected', {
                detail: selectedItem,
              })
            );
          }

          setIsOpen(false);
          return true;
        }

      }

      return false;
    },
    [
      isOpen,
      activeResultsCount,
      triggerType,
      slashResults,
      dynamicMentions,
      selectedIndex,
      editor,
      queryString,
      onClearEditor,
    ]
  );

  useEffect(() => {
    const unregisterEnter = editor.registerCommand(
      KEY_ENTER_COMMAND,
      (event) => {
        if (isOpen && activeResultsCount > 0) {
          if (event) event.preventDefault();
          return handleExecuteSelection();
        }
        return false;
      },
      COMMAND_PRIORITY_CRITICAL
    );

    const unregisterTab = editor.registerCommand(
      KEY_TAB_COMMAND,
      (event) => {
        if (isOpen && activeResultsCount > 0) {
          if (event) event.preventDefault();
          return handleExecuteSelection();
        }
        return false;
      },
      COMMAND_PRIORITY_CRITICAL
    );

    const unregisterDown = editor.registerCommand(
      KEY_ARROW_DOWN_COMMAND,
      (event) => {
        if (isOpen && activeResultsCount > 0) {
          if (event) event.preventDefault();
          setSelectedIndex((prev) => (prev + 1) % activeResultsCount);
          return true;
        }
        return false;
      },
      COMMAND_PRIORITY_CRITICAL
    );

    const unregisterUp = editor.registerCommand(
      KEY_ARROW_UP_COMMAND,
      (event) => {
        if (isOpen && activeResultsCount > 0) {
          if (event) event.preventDefault();
          setSelectedIndex(
            (prev) => (prev - 1 + activeResultsCount) % activeResultsCount
          );
          return true;
        }
        return false;
      },
      COMMAND_PRIORITY_CRITICAL
    );

    const unregisterEscape = editor.registerCommand(
      KEY_ESCAPE_COMMAND,
      (event) => {
        if (isOpen) {
          if (event) event.preventDefault();
          setIsOpen(false);
          return true;
        }
        return false;
      },
      COMMAND_PRIORITY_CRITICAL
    );

    return () => {
      unregisterEnter();
      unregisterTab();
      unregisterDown();
      unregisterUp();
      unregisterEscape();
    };
  }, [editor, isOpen, activeResultsCount, handleExecuteSelection]);

  if (!isOpen) return null;

  return (
    <div
      ref={menuRef}
      className="absolute bottom-full mb-2 start-0 z-[9999] w-full max-h-[32rem] overflow-y-auto rounded-2xl border border-border/80 bg-popover/95 p-1.5 text-popover-foreground shadow-2xl backdrop-blur-md animate-in fade-in zoom-in-95 duration-150 font-sans"
    >
      <div className="sticky top-0 z-10 flex items-center justify-between border-b border-border/50 bg-popover/95 px-3 py-2">
        <div className="flex items-center gap-1 min-w-0 flex-1">
          {triggerType === 'mention' && currentPath && (
            <button
              type="button"
              onMouseDown={(e) => e.preventDefault()}
              onClick={(e) => {
                e.preventDefault();
                const parts = currentPath.split('/').filter(Boolean);
                parts.pop();
                setCurrentPath(parts.join('/'));
                setSelectedIndex(0);
              }}
              className="me-1 flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
              title="Go up one folder"
              aria-label="Go up one folder"
            >
              <ChevronLeft size={15} />
            </button>
          )}
          <span className="truncate text-sm font-semibold text-foreground">
            {triggerType === 'slash' ? 'Slash Commands' : 'Files & folders'}
          </span>
          {triggerType === 'mention' && currentPath && (
            <span className="ms-2 truncate text-xs font-normal text-muted-foreground">
              {currentPath}
            </span>
          )}
        </div>
        <span className="ms-2 shrink-0 text-[11px] text-muted-foreground">
          ↑ ↓ to navigate · Enter to select
        </span>
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center gap-2 py-6 text-sm text-muted-foreground">
          <span className="h-4 w-4 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-primary" />
          <span>Loading files and folders...</span>
        </div>
      ) : activeResultsCount === 0 ? (
        <div className="py-8 text-center text-sm text-muted-foreground">
          {triggerType === 'slash'
            ? 'No commands found'
            : 'No files or folders found'}
        </div>
      ) : (
        <div className="flex flex-col gap-0.5">
          {triggerType === 'slash' &&
            slashResults.map((item, index) => {
              const isSelected = index === selectedIndex;
              return (
                <button
                  key={item.id}
                  type="button"
                  onMouseDown={(e) => e.preventDefault()}
                  onMouseEnter={() => setSelectedIndex(index)}
                  onClick={(e) => {
                    e.preventDefault();
                    handleExecuteSelection(index);
                  }}
                  className={`flex items-center gap-2.5 w-full px-2.5 py-1.5 rounded-lg text-start text-xs transition-colors cursor-pointer ${
                    isSelected
                      ? 'bg-primary text-primary-foreground font-medium'
                      : 'hover:bg-muted text-foreground'
                  }`}
                >
                  <div
                    className={`p-1 rounded-md shrink-0 ${
                      isSelected ? 'bg-primary-foreground/20' : 'bg-muted'
                    }`}
                  >
                    {item.category === 'agent' ? (
                      <Bot size={13} />
                    ) : item.category === 'tool' ? (
                      <Wrench size={13} />
                    ) : item.category === 'skill' ? (
                      <Sparkles size={13} />
                    ) : (
                      <Terminal size={13} />
                    )}
                  </div>
                  <div className="flex flex-col min-w-0 flex-1">
                    <span className="font-mono font-medium truncate">
                      {item.label}
                    </span>
                    <span
                      className={`text-[10px] truncate ${
                        isSelected
                          ? 'text-primary-foreground/80'
                          : 'text-muted-foreground'
                      }`}
                    >
                      {item.description}
                    </span>
                  </div>
                </button>
              );
            })}

          {triggerType === 'mention' &&
            dynamicMentions.map((item, index) => {
              const isSelected = index === selectedIndex;
              const isFolder = item.category === 'folder';
              return (
                <button
                  key={item.id}
                  type="button"
                  title={item.description || item.label}
                  onMouseDown={(e) => e.preventDefault()}
                  onMouseEnter={() => setSelectedIndex(index)}
                  onClick={(e) => {
                    e.preventDefault();
                    handleExecuteSelection(index);
                  }}
                  className={`flex min-h-10 w-full items-center gap-3 rounded-xl px-3 py-2 text-start text-sm transition-colors cursor-pointer ${
                    isSelected
                      ? 'bg-muted/80 text-foreground'
                      : 'text-foreground hover:bg-muted/60'
                  }`}
                >
                  <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-muted/70">
                    {isFolder ? (
                      <Folder size={17} className="text-muted-foreground" />
                    ) : (
                      <FileText size={17} className="text-muted-foreground" />
                    )}
                  </span>
                  <span className="min-w-0 flex-1 truncate font-medium">
                    {item.label}{isFolder ? '/' : ''}
                  </span>
                  {isSelected && isFolder && (
                    <span className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
                      <span>Browse folder</span>
                      <kbd className="rounded border border-border bg-background px-1.5 py-0.5 font-mono text-[10px]">
                        Tab
                      </kbd>
                    </span>
                  )}
                  {isSelected && !isFolder && (
                    <span className="shrink-0 text-xs text-muted-foreground">
                      Enter to add
                    </span>
                  )}
                  {isFolder && <ChevronRight size={15} className="shrink-0 text-muted-foreground/70" />}
                </button>
              );
            })}
        </div>
      )}
    </div>
  );
}

export default TypeaheadMenuPlugin;
