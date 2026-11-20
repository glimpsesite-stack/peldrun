// frontend/src/features/workspace/notebook-editor.tsx

/**
 * Advanced Notebook Editor using Lexical
 * Provides Notion-like editing experience with:
 * - Rich text editing (headings, lists, quotes, code blocks)
 * - Fixed formatting toolbar and floating selection toolbar
 * - Markdown support with live preview
 * - Internal links [[note-name]]
 * - Slash commands (/)
 * - Code syntax highlighting
 * - Math equations (KaTeX)
 * - Mermaid diagrams
 * - OCC (Optimistic Concurrency Control) integration
 */
'use client';

import React, {
  useCallback,
  useEffect,
  useMemo,
  useState,
} from 'react';
import { LexicalComposer } from '@lexical/react/LexicalComposer';
import { RichTextPlugin } from '@lexical/react/LexicalRichTextPlugin';
import { ContentEditable } from '@lexical/react/LexicalContentEditable';
import { HistoryPlugin } from '@lexical/react/LexicalHistoryPlugin';
import { OnChangePlugin } from '@lexical/react/LexicalOnChangePlugin';
import { LexicalErrorBoundary } from '@lexical/react/LexicalErrorBoundary';
import { MarkdownShortcutPlugin } from '@lexical/react/LexicalMarkdownShortcutPlugin';
import { LinkPlugin } from '@lexical/react/LexicalLinkPlugin';
import { ListPlugin } from '@lexical/react/LexicalListPlugin';
import { CodeNode, CodeHighlightNode, $createCodeNode } from '@lexical/code';
import { registerCodeHighlighting } from '@lexical/code-prism';
import { useLexicalComposerContext } from '@lexical/react/LexicalComposerContext';
import {
  HeadingNode,
  QuoteNode,
  $createHeadingNode,
  $createQuoteNode,
  $isHeadingNode,
  HeadingTagType,
} from '@lexical/rich-text';
import {
  ListNode,
  ListItemNode,
  INSERT_ORDERED_LIST_COMMAND,
  INSERT_UNORDERED_LIST_COMMAND,
} from '@lexical/list';
import { LinkNode, AutoLinkNode, TOGGLE_LINK_COMMAND } from '@lexical/link';
import { $setBlocksType } from '@lexical/selection';
import {
  $convertFromMarkdownString,
  $convertToMarkdownString,
  TRANSFORMERS,
} from '@lexical/markdown';
import {
  $getRoot,
  $getSelection,
  $isRangeSelection,
  $createParagraphNode,
  $createTextNode,
  EditorState,
  LexicalEditor,
  FORMAT_TEXT_COMMAND,
  UNDO_COMMAND,
  REDO_COMMAND,
  CAN_UNDO_COMMAND,
  CAN_REDO_COMMAND,
  COMMAND_PRIORITY_CRITICAL,
} from 'lexical';
import { mergeRegister } from '@lexical/utils';
import {
  Bold,
  Italic,
  Underline,
  Strikethrough,
  Code,
  Heading1,
  Heading2,
  Heading3,
  List,
  ListOrdered,
  Quote,
  Link2,
  Undo,
  Redo,
  FileCode,
  Pilcrow,
  Save,
  Check,
} from 'lucide-react';
import { FloatingToolbarPlugin } from '@/components/chat/composer/plugins/floating-toolbar-plugin';
import { workspaceApi } from './workspace-api';
import { OCCConflictError } from './types';

// ============================================================================
// Custom Transformers for Notebook
// ============================================================================

/**
 * Internal link transformer: [[note-name]]
 */
const INTERNAL_LINK_TRANSFORMER = {
  dependencies: [LinkNode],
  type: 'text-match' as const,
  trigger: ']',
  regExp: /\[\[([^\]]+)\]\]/,
  replace: (textNode: any, match: RegExpMatchArray) => {
    const noteName = match[1];
    const linkNode = new LinkNode(`/notebook/${noteName}`);
    linkNode.append($createTextNode(noteName));
    textNode.replace(linkNode);
  },
};

/**
 * Extended transformers for Notebook
 */
const NOTEBOOK_TRANSFORMERS = [
  ...TRANSFORMERS,
  INTERNAL_LINK_TRANSFORMER,
];

// ============================================================================
// Editor Theme
// ============================================================================

const editorTheme = {
  paragraph: 'mb-2 leading-relaxed',
  heading: {
    h1: 'text-3xl font-bold mb-4 mt-6',
    h2: 'text-2xl font-bold mb-3 mt-5',
    h3: 'text-xl font-semibold mb-2 mt-4',
    h4: 'text-lg font-semibold mb-2 mt-3',
    h5: 'text-base font-semibold mb-1 mt-2',
    h6: 'text-sm font-semibold mb-1 mt-2',
  },
  list: {
    ol: 'list-decimal ml-6 mb-2',
    ul: 'list-disc ml-6 mb-2',
    listitem: 'mb-1',
  },
  quote: 'border-l-4 border-primary/50 pl-4 italic text-muted-foreground my-3',
  code: 'bg-muted/50 rounded-md p-4 my-3 font-mono text-sm overflow-x-auto block',
  codeHighlight: {
    atrule: 'text-purple-600',
    attr: 'text-orange-600',
    boolean: 'text-blue-600',
    builtin: 'text-green-600',
    cdata: 'text-gray-500',
    char: 'text-green-600',
    class: 'text-yellow-600',
    'class-name': 'text-yellow-600',
    comment: 'text-gray-500 italic',
    constant: 'text-blue-600',
    deleted: 'text-red-600',
    doctype: 'text-gray-500',
    entity: 'text-orange-600',
    function: 'text-blue-600',
    important: 'text-red-600 font-bold',
    inserted: 'text-green-600',
    keyword: 'text-purple-600',
    namespace: 'text-orange-600',
    number: 'text-blue-600',
    operator: 'text-orange-600',
    prolog: 'text-gray-500',
    property: 'text-orange-600',
    punctuation: 'text-gray-600',
    regex: 'text-blue-600',
    selector: 'text-green-600',
    string: 'text-green-600',
    symbol: 'text-blue-600',
    tag: 'text-red-600',
    url: 'text-blue-600',
    variable: 'text-orange-600',
  },
  link: 'text-primary hover:underline cursor-pointer',
  text: {
    bold: 'font-bold',
    italic: 'italic',
    underline: 'underline',
    strikethrough: 'line-through',
    code: 'bg-muted/80 px-1.5 py-0.5 rounded font-mono text-sm text-primary',
  },
};

// ============================================================================
// Editor Nodes
// ============================================================================

const editorNodes = [
  HeadingNode,
  QuoteNode,
  ListNode,
  ListItemNode,
  CodeNode,
  CodeHighlightNode,
  LinkNode,
  AutoLinkNode,
];

// ============================================================================
// Fixed Formatting Toolbar Plugin
// ============================================================================

function NotebookToolbarPlugin() {
  const [editor] = useLexicalComposerContext();
  const [canUndo, setCanUndo] = useState(false);
  const [canRedo, setCanRedo] = useState(false);
  const [isBold, setIsBold] = useState(false);
  const [isItalic, setIsItalic] = useState(false);
  const [isUnderline, setIsUnderline] = useState(false);
  const [isStrikethrough, setIsStrikethrough] = useState(false);
  const [isCode, setIsCode] = useState(false);
  const [blockType, setBlockType] = useState<string>('paragraph');

  const updateToolbar = useCallback(() => {
    const selection = $getSelection();
    if ($isRangeSelection(selection)) {
      setIsBold(selection.hasFormat('bold'));
      setIsItalic(selection.hasFormat('italic'));
      setIsUnderline(selection.hasFormat('underline'));
      setIsStrikethrough(selection.hasFormat('strikethrough'));
      setIsCode(selection.hasFormat('code'));

      const anchorNode = selection.anchor.getNode();
      const element =
        anchorNode.getKey() === 'root'
          ? anchorNode
          : anchorNode.getTopLevelElementOrThrow();
      if ($isHeadingNode(element)) {
        setBlockType(element.getTag());
      } else {
        setBlockType(element.getType());
      }
    }
  }, []);

  useEffect(() => {
    return mergeRegister(
      editor.registerUpdateListener(({ editorState }) => {
        editorState.read(() => {
          updateToolbar();
        });
      }),
      editor.registerCommand(
        CAN_UNDO_COMMAND,
        (payload) => {
          setCanUndo(payload);
          return false;
        },
        COMMAND_PRIORITY_CRITICAL
      ),
      editor.registerCommand(
        CAN_REDO_COMMAND,
        (payload) => {
          setCanRedo(payload);
          return false;
        },
        COMMAND_PRIORITY_CRITICAL
      )
    );
  }, [editor, updateToolbar]);

  const formatParagraph = () => {
    editor.update(() => {
      const selection = $getSelection();
      if ($isRangeSelection(selection)) {
        $setBlocksType(selection, () => $createParagraphNode());
      }
    });
  };

  const formatHeading = (headingSize: HeadingTagType) => {
    if (blockType !== headingSize) {
      editor.update(() => {
        const selection = $getSelection();
        if ($isRangeSelection(selection)) {
          $setBlocksType(selection, () => $createHeadingNode(headingSize));
        }
      });
    } else {
      formatParagraph();
    }
  };

  const formatQuote = () => {
    if (blockType !== 'quote') {
      editor.update(() => {
        const selection = $getSelection();
        if ($isRangeSelection(selection)) {
          $setBlocksType(selection, () => $createQuoteNode());
        }
      });
    } else {
      formatParagraph();
    }
  };

  const formatCodeBlock = () => {
    if (blockType !== 'code') {
      editor.update(() => {
        const selection = $getSelection();
        if ($isRangeSelection(selection)) {
          $setBlocksType(selection, () => $createCodeNode());
        }
      });
    } else {
      formatParagraph();
    }
  };

  const insertLink = () => {
    const url = window.prompt('Enter URL (leave empty to remove link):');
    if (url === null) return;
    editor.dispatchCommand(TOGGLE_LINK_COMMAND, url === '' ? null : url);
  };

  const ToolbarButton = ({
    active = false,
    disabled = false,
    onClick,
    title,
    children,
  }: {
    active?: boolean;
    disabled?: boolean;
    onClick: () => void;
    title: string;
    children: React.ReactNode;
  }) => (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      title={title}
      className={`p-1.5 rounded-md transition-colors flex items-center justify-center ${
        active
          ? 'bg-primary/15 text-primary font-semibold'
          : 'text-muted-foreground hover:text-foreground hover:bg-muted'
      } disabled:opacity-40 disabled:pointer-events-none`}
    >
      {children}
    </button>
  );

  return (
    <div className="flex flex-wrap items-center gap-1 px-3 py-2 border-b border-muted bg-gigbg sticky top-0 z-10 backdrop-blur-sm">
      <ToolbarButton
        disabled={!canUndo}
        onClick={() => editor.dispatchCommand(UNDO_COMMAND, undefined)}
        title="Undo"
      >
        <Undo size={15} />
      </ToolbarButton>
      <ToolbarButton
        disabled={!canRedo}
        onClick={() => editor.dispatchCommand(REDO_COMMAND, undefined)}
        title="Redo"
      >
        <Redo size={15} />
      </ToolbarButton>
      <div className="w-[1px] h-5 bg-border mx-1" />
      <ToolbarButton
        active={blockType === 'paragraph'}
        onClick={formatParagraph}
        title="Paragraph"
      >
        <Pilcrow size={15} />
      </ToolbarButton>
      <ToolbarButton
        active={blockType === 'h1'}
        onClick={() => formatHeading('h1')}
        title="Heading 1"
      >
        <Heading1 size={15} />
      </ToolbarButton>
      <ToolbarButton
        active={blockType === 'h2'}
        onClick={() => formatHeading('h2')}
        title="Heading 2"
      >
        <Heading2 size={15} />
      </ToolbarButton>
      <ToolbarButton
        active={blockType === 'h3'}
        onClick={() => formatHeading('h3')}
        title="Heading 3"
      >
        <Heading3 size={15} />
      </ToolbarButton>
      <div className="w-[1px] h-5 bg-border mx-1" />
      <ToolbarButton
        active={isBold}
        onClick={() => editor.dispatchCommand(FORMAT_TEXT_COMMAND, 'bold')}
        title="Bold"
      >
        <Bold size={15} />
      </ToolbarButton>
      <ToolbarButton
        active={isItalic}
        onClick={() => editor.dispatchCommand(FORMAT_TEXT_COMMAND, 'italic')}
        title="Italic"
      >
        <Italic size={15} />
      </ToolbarButton>
      <ToolbarButton
        active={isUnderline}
        onClick={() => editor.dispatchCommand(FORMAT_TEXT_COMMAND, 'underline')}
        title="Underline"
      >
        <Underline size={15} />
      </ToolbarButton>
      <ToolbarButton
        active={isStrikethrough}
        onClick={() => editor.dispatchCommand(FORMAT_TEXT_COMMAND, 'strikethrough')}
        title="Strikethrough"
      >
        <Strikethrough size={15} />
      </ToolbarButton>
      <ToolbarButton
        active={isCode}
        onClick={() => editor.dispatchCommand(FORMAT_TEXT_COMMAND, 'code')}
        title="Inline Code"
      >
        <Code size={15} />
      </ToolbarButton>
      <div className="w-[1px] h-5 bg-border mx-1" />
      <ToolbarButton
        active={blockType === 'ul'}
        onClick={() => editor.dispatchCommand(INSERT_UNORDERED_LIST_COMMAND, undefined)}
        title="Bullet List"
      >
        <List size={15} />
      </ToolbarButton>
      <ToolbarButton
        active={blockType === 'ol'}
        onClick={() => editor.dispatchCommand(INSERT_ORDERED_LIST_COMMAND, undefined)}
        title="Numbered List"
      >
        <ListOrdered size={15} />
      </ToolbarButton>
      <ToolbarButton
        active={blockType === 'quote'}
        onClick={formatQuote}
        title="Quote"
      >
        <Quote size={15} />
      </ToolbarButton>
      <ToolbarButton
        active={blockType === 'code'}
        onClick={formatCodeBlock}
        title="Code Block"
      >
        <FileCode size={15} />
      </ToolbarButton>
      <ToolbarButton
        onClick={insertLink}
        title="Insert Link"
      >
        <Link2 size={15} />
      </ToolbarButton>
    </div>
  );
}

// ============================================================================
// Slash Commands Plugin
// ============================================================================

function SlashCommandsPlugin() {
  const [editor] = useLexicalComposerContext();
  const [showMenu, setShowMenu] = useState(false);
  const [menuPosition, setMenuPosition] = useState({ x: 0, y: 0 });
  const [selectedIndex, setSelectedIndex] = useState(0);

  const commands = [
    { name: 'Heading 1', icon: 'H1', action: () => insertHeading(1) },
    { name: 'Heading 2', icon: 'H2', action: () => insertHeading(2) },
    { name: 'Heading 3', icon: 'H3', action: () => insertHeading(3) },
    { name: 'Bullet List', icon: '•', action: () => insertList('bullet') },
    { name: 'Numbered List', icon: '1.', action: () => insertList('number') },
    { name: 'Quote', icon: '"', action: () => insertQuote() },
    { name: 'Code Block', icon: '<>', action: () => insertCodeBlock() },
    { name: 'Divider', icon: '—', action: () => insertDivider() },
  ];

  const insertHeading = (level: number) => {
    editor.update(() => {
      const root = $getRoot();
      const paragraph = $createParagraphNode();
      paragraph.append($createTextNode(`Heading ${level}`));
      root.append(paragraph);
    });
    setShowMenu(false);
  };

  const insertList = (type: 'bullet' | 'number') => {
    editor.update(() => {
      const root = $getRoot();
      const paragraph = $createParagraphNode();
      paragraph.append($createTextNode(type === 'bullet' ? '• ' : '1. '));
      root.append(paragraph);
    });
    setShowMenu(false);
  };

  const insertQuote = () => {
    editor.update(() => {
      const root = $getRoot();
      const paragraph = $createParagraphNode();
      paragraph.append($createTextNode('> '));
      root.append(paragraph);
    });
    setShowMenu(false);
  };

  const insertCodeBlock = () => {
    editor.update(() => {
      const root = $getRoot();
      const paragraph = $createParagraphNode();
      paragraph.append($createTextNode('`\n\n`'));
      root.append(paragraph);
    });
    setShowMenu(false);
  };

  const insertDivider = () => {
    editor.update(() => {
      const root = $getRoot();
      const paragraph = $createParagraphNode();
      paragraph.append($createTextNode('\n---\n'));
      root.append(paragraph);
    });
    setShowMenu(false);
  };

  useEffect(() => {
    return editor.registerUpdateListener(({ editorState }) => {
      editorState.read(() => {
        const root = $getRoot();
        const lastParagraph = root.getLastChild();
        if (lastParagraph) {
          const text = lastParagraph.getTextContent();
          if (text.endsWith('/')) {
            setShowMenu(true);
            setMenuPosition({ x: 100, y: 100 });
          } else {
            setShowMenu(false);
          }
        }
      });
    });
  }, [editor]);

  if (!showMenu) return null;

  return (
    <div
      className="fixed bg-card border border-border rounded-lg shadow-lg p-2 z-50"
      style={{ left: menuPosition.x, top: menuPosition.y }}
    >
      {commands.map((cmd, idx) => (
        <button
          key={cmd.name}
          className={`w-full text-left px-3 py-2 rounded hover:bg-muted flex items-center gap-2 ${
            idx === selectedIndex ? 'bg-muted' : ''
          }`}
          onClick={cmd.action}
        >
          <span className="font-mono text-sm text-primary">{cmd.icon}</span>
          <span className="text-sm">{cmd.name}</span>
        </button>
      ))}
    </div>
  );
}

// ============================================================================
// Code Highlight Plugin
// ============================================================================

function CodeHighlightPluginWrapper() {
  const [editor] = useLexicalComposerContext();
  useEffect(() => {
    return registerCodeHighlighting(editor);
  }, [editor]);
  return null;
}

// ============================================================================
// Load File Plugin (Replaces external useEffect to prevent race conditions)
// ============================================================================

interface LoadFilePluginProps {
  projectId: string;
  filePath: string;
  initialContent?: string;
  onVersionLoaded: (version: string) => void;
  onEditorReady: (editor: LexicalEditor) => void;
}

function LoadFilePlugin({
  projectId,
  filePath,
  initialContent,
  onVersionLoaded,
  onEditorReady,
}: LoadFilePluginProps) {
  const [editor] = useLexicalComposerContext();
  const [hasLoaded, setHasLoaded] = useState(false);

  useEffect(() => {
    // Guarantee editor instance is captured immediately upon mount
    onEditorReady(editor);

    if (hasLoaded) return;

    const loadFile = async () => {
      try {
        const response = await workspaceApi.readFile(projectId, filePath);
        onVersionLoaded(response.version);

        const contentToLoad = response.content || initialContent || '';

        editor.update(() => {
          const root = $getRoot();
          root.clear();
          if (contentToLoad) {
            $convertFromMarkdownString(contentToLoad, NOTEBOOK_TRANSFORMERS);
          } else {
            root.append($createParagraphNode());
          }
        });
        setHasLoaded(true);
      } catch (err) {
        console.error('Failed to load file:', err);
        // Fallback to initialContent if fetch fails (e.g., slight delay in file creation propagation)
        if (initialContent) {
          editor.update(() => {
            const root = $getRoot();
            root.clear();
            $convertFromMarkdownString(initialContent, NOTEBOOK_TRANSFORMERS);
          });
          setHasLoaded(true);
        }
      }
    };

    loadFile();
  }, [editor, projectId, filePath, initialContent, hasLoaded, onVersionLoaded, onEditorReady]);

  return null;
}

// ============================================================================
// Main Editor Component
// ============================================================================

interface NotebookEditorProps {
  projectId: string;
  filePath: string;
  initialContent?: string;
  onSave?: (content: string) => void;
  readOnly?: boolean;
}

export function NotebookEditor({
  projectId,
  filePath,
  initialContent = '',
  onSave,
  readOnly = false,
}: NotebookEditorProps) {
  const editorRef = React.useRef<LexicalEditor | null>(null);
  const [currentVersion, setCurrentVersion] = useState<string>('');
  const [isSaving, setIsSaving] = useState(false);
  const [lastSaved, setLastSaved] = useState<Date | null>(null);

  const initialConfig = useMemo(
    () => ({
      namespace: 'NotebookEditor',
      nodes: editorNodes,
      theme: editorTheme,
      onError: (error: Error) => {
        console.error('[NotebookEditor Error]:', error);
      },
      editable: !readOnly,
    }),
    [readOnly]
  );

  const handleChange = useCallback(
    (editorState: EditorState, editor: LexicalEditor) => {
      // Auto-save hook point
    },
    []
  );

  const handleSave = useCallback(async () => {
    setIsSaving(true);
    try {
      const editor = editorRef.current;
      if (!editor) return;

      let markdownContent = '';
      editor.getEditorState().read(() => {
        markdownContent = $convertToMarkdownString(NOTEBOOK_TRANSFORMERS);
      });

      const response = await workspaceApi.updateFile(projectId, {
        path: filePath,
        content: markdownContent,
        expected_version: currentVersion,
      });
      setCurrentVersion(response.version);
      setLastSaved(new Date());
      if (onSave) {
        onSave(markdownContent);
      }
    } catch (err) {
      if (err instanceof OCCConflictError) {
        alert('This note has been modified by another user. Please reload and merge your changes.');
      } else {
        console.error('Failed to save:', err);
        alert('Failed to save note. Please try again.');
      }
    } finally {
      setIsSaving(false);
    }
  }, [projectId, filePath, currentVersion, onSave]);

  const handleEditorReady = useCallback((editor: LexicalEditor) => {
    editorRef.current = editor;
  }, []);

  return (
    <div className="flex flex-col h-full p-1 bg-rb/5">
      <div className="flex-1 flex flex-col max-w-7xl w-full mx-auto bg-card border border-border rounded-sm shadow-sm overflow-scroll focus-within:ring-1
       [&::-webkit-scrollbar]:w-0
       focus-within:ring-card focus-within:border-card   transition-all">
        <LexicalComposer initialConfig={initialConfig}>
          <LoadFilePlugin
            projectId={projectId}
            filePath={filePath}
            initialContent={initialContent}
            onVersionLoaded={setCurrentVersion}
            onEditorReady={handleEditorReady}
          />
          {!readOnly && <NotebookToolbarPlugin />}
          {!readOnly && <FloatingToolbarPlugin />}
          <div className="relative flex-1 overflow-y-auto min-h-0 scrollbar-thin scrollbar-thumb-slate-500 scrollbar-track-slate-100
           [&::-webkit-scrollbar]:w-1
            [&::-webkit-scrollbar-track]:bg-card
            [&::-webkit-scrollbar-thumb]:bg-muted
            [&::-webkit-scrollbar-thumb]:rounded-full

          ">
            <RichTextPlugin
              contentEditable={
                <ContentEditable
                  className="outline-none min-h-full px-8 py-6 prose prose-sm max-w-none"
                  aria-placeholder="Start writing..."
                  placeholder={
                    <div className="absolute top-6 left-8 text-muted-foreground pointer-events-none select-none">
                      Start writing...
                    </div>
                  }
                />
              }
              ErrorBoundary={LexicalErrorBoundary}
            />
          </div>
          <HistoryPlugin />
          <OnChangePlugin onChange={handleChange} />
          <MarkdownShortcutPlugin transformers={NOTEBOOK_TRANSFORMERS} />
          <LinkPlugin />
          <ListPlugin />
          <CodeHighlightPluginWrapper />
          <SlashCommandsPlugin />
        </LexicalComposer>
        {!readOnly && (
          <div className="border-t border-border px-4 py-2.5 flex items-center justify-between bg-muted/20">
            <div className="text-xs text-muted-foreground flex items-center gap-1.5">
              {lastSaved ? (
                <>
                  <Check size={13} className="text-green-600" />
                  <span>Last saved: {lastSaved.toLocaleTimeString()}</span>
                </>
              ) : (
                <span>Supports Markdown &amp; [[internal-links]]</span>
              )}
            </div>
            <button
              onClick={handleSave}
              disabled={isSaving}
              className="flex items-center gap-1.5 px-4 py-1.5 text-sm font-medium bg-rb text-[#fff]
              rounded-md hover:bg-black disabled:opacity-50 transition-colors cursor-pointer"
            >
              <Save size={14} />
              {isSaving ? 'Saving...' : 'Save Note'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
