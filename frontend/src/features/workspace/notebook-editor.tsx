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
 * - Recharts, Images, Tables
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
import { TablePlugin } from '@lexical/react/LexicalTablePlugin';
import {
  TableNode,
  TableRowNode,
  TableCellNode,
  INSERT_TABLE_COMMAND,
  $createTableNodeWithDimensions,
  $isTableNode,
  $isTableRowNode,
  $isTableCellNode,
} from '@lexical/table';
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
  type MultilineElementTransformer,
} from '@lexical/markdown';
import {
  $getRoot,
  $getSelection,
  $setSelection,
  $isRangeSelection,
  $createParagraphNode,
  $createTextNode,
  $insertNodes,
  EditorState,
  LexicalEditor,
  type LexicalNode,
  createEditor,
  FORMAT_TEXT_COMMAND,
  UNDO_COMMAND,
  REDO_COMMAND,
  CAN_UNDO_COMMAND,
  CAN_REDO_COMMAND,
  COPY_COMMAND,
  PASTE_COMMAND,
  COMMAND_PRIORITY_HIGH,
  COMMAND_PRIORITY_CRITICAL,
  DecoratorNode,
  DOMConversionMap,
  DOMExportOutput,
  EditorConfig,
  NodeKey,
  SerializedLexicalNode,
  Spread,
} from 'lexical';

import {
  $generateHtmlFromNodes,
  $generateNodesFromDOM,
} from '@lexical/html';
import { mergeRegister } from '@lexical/utils';
import {
  Bold,
  Italic,
  Sigma,
  Network,
  BarChart3,
  PanelRightOpen,
  PanelRightClose,
  Underline,
  Strikethrough,
  Code,
  Image as ImageIcon,
  Images,
  Upload,
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
  Table2,
} from 'lucide-react';
import { FloatingToolbarPlugin } from '@/components/chat/composer/plugins/floating-toolbar-plugin';
import { workspaceApi } from './workspace-api';
import { OCCConflictError } from './types';
import { MarkdownRenderer } from '@/components/chat/markdown-renderer';
import { Button } from '@/components/ui/button';

// ============================================================================
// Global State
// ============================================================================

// Track the active editor instance so transformers can access it for
// HTML-to-Nodes conversion during import.
let activeEditor: LexicalEditor | null = null;

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

const MARKDOWN_TABLE_ROW_REGEX = /^\s*\|.*\|\s*$/;

// Fixes text that was accidentally escaped by Lexical's exporter
function unescapeMarkdownText(text: string): string {
  return text
    .replace(/\\\*/g, '*')
    .replace(/\\_/g, '_')
    .replace(/\\`/g, '`');
}

const MARKDOWN_TABLE_DIVIDER_REGEX =
  /^\s*\|\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|\s*$/;

function parseMarkdownTableRow(line: string): string[] {
  const trimmed = line.trim();
  const content = trimmed.slice(1, -1);
  const cells: string[] = [];
  let cell = '';

  for (let index = 0; index < content.length; index += 1) {
    const char = content[index];

    if (char === '\\' && content[index + 1] === '|') {
      cell += '|';
      index += 1;
      continue;
    }

    if (char === '|') {
      cells.push(cell.trim());
      cell = '';
      continue;
    }

    cell += char;
  }

  cells.push(cell.trim());

  return cells;
}

const MARKDOWN_TABLE_TRANSFORMER: MultilineElementTransformer = {
  dependencies: [TableNode],

  regExpStart: MARKDOWN_TABLE_ROW_REGEX,

  handleImportAfterStartMatch: ({
    lines,
    rootNode,
    startLineIndex,
  }) => {
    const dividerIndex = startLineIndex + 1;

    if (
      dividerIndex >= lines.length ||
      !MARKDOWN_TABLE_DIVIDER_REGEX.test(lines[dividerIndex])
    ) {
      return [false, startLineIndex];
    }

    const rows: string[][] = [
      parseMarkdownTableRow(lines[startLineIndex]),
    ];

    let lastLineIndex = dividerIndex;

    for (
      let lineIndex = dividerIndex + 1;
      lineIndex < lines.length;
      lineIndex += 1
    ) {
      const line = lines[lineIndex];

      if (
        !MARKDOWN_TABLE_ROW_REGEX.test(line) ||
        MARKDOWN_TABLE_DIVIDER_REGEX.test(line)
      ) {
        break;
      }

      rows.push(parseMarkdownTableRow(line));
      lastLineIndex = lineIndex;
    }

    const columnCount = Math.max(
      1,
      ...rows.map((row) => row.length),
    );

    const tableNode = $createTableNodeWithDimensions(
      rows.length,
      columnCount,
      {
        rows: true,
        columns: false,
      },
    );

    tableNode.getChildren().forEach((rowNode, rowIndex) => {
      if (!$isTableRowNode(rowNode)) {
        return;
      }

      rowNode.getChildren().forEach((cellNode, columnIndex) => {
        if (!$isTableCellNode(cellNode)) {
          return;
        }

        cellNode.clear();

        const rawCellMarkdown = rows[rowIndex]?.[columnIndex] ?? '';
        const cellMarkdown = unescapeMarkdownText(rawCellMarkdown);

        if (activeEditor && cellMarkdown.trim()) {
          try {
            const cellHtml = markdownToHtml(cellMarkdown);
            if (cellHtml) {
              const parser = new DOMParser();
              const dom = parser.parseFromString(cellHtml, 'text/html');
              const nodes = $generateNodesFromDOM(activeEditor, dom);
              if (nodes.length > 0) {
                cellNode.append(...nodes);
                return;
              }
            }
          } catch (err) {
            console.error('[NotebookTable] Failed to parse cell markdown:', err);
          }
        }

        cellNode.append(
          $createParagraphNode().append($createTextNode(cellMarkdown))
        );
      });
    });

    rootNode.append(tableNode);

    return [true, lastLineIndex];
  },

  export: (node, exportChildren) => {
    if (!$isTableNode(node)) {
      return null;
    }

    const rows = node
      .getChildren()
      .filter($isTableRowNode)
      .map((row) => row.getChildren().filter($isTableCellNode));

    if (rows.length === 0) {
      return null;
    }

    const columnCount = Math.max(
      1,
      ...rows.map((row) => row.length),
    );

    const renderRow = (cells: TableCellNode[]) => {
      const values = Array.from(
        { length: columnCount },
        (_, index) => {
          const cell = cells[index];

          if (!cell) {
            return '';
          }

          return exportChildren(cell)
            .replace(/\r?\n/g, ' ')
            .replace(/\|/g, '\\|')
            .trim();
        },
      );

      return `| ${values.join(' | ')} |`;
    };

    const header = renderRow(rows[0]);

    const divider = `| ${
      Array.from(
        { length: columnCount },
        () => '---',
      ).join(' | ')
    } |`;

    return [
      header,
      divider,
      ...rows.slice(1).map(renderRow),
    ].join('\n');
  },

  replace: () => false,

  type: 'multiline-element',
};

// ============================================================================
// Custom Image Node
// ============================================================================

export type SerializedImageNode = Spread<
  { src: string; altText: string },
  SerializedLexicalNode
>;

export class ImageNode extends DecoratorNode<React.JSX.Element> {
  __src: string;
  __altText: string;

  static getType(): string {
    return 'image';
  }

  static clone(node: ImageNode): ImageNode {
    return new ImageNode(node.__src, node.__altText, node.__key);
  }

  constructor(src: string = '', altText: string = '', key?: NodeKey) {
    super(key);
    this.__src = src;
    this.__altText = altText;
  }

  createDOM(config: EditorConfig): HTMLElement {
    const span = document.createElement('span');
    span.className = 'inline-block max-w-full my-2';
    return span;
  }

  updateDOM(): boolean {
    return false;
  }

  exportDOM(): DOMExportOutput {
    const img = document.createElement('img');
    img.setAttribute('src', this.__src);
    img.setAttribute('alt', this.__altText);
    return { element: img };
  }

  static importDOM(): DOMConversionMap | null {
    return {
      img: () => ({
        conversion: (domNode: HTMLElement) => {
          const img = domNode as HTMLImageElement;
          if (!img.src) return null;
          return { node: new ImageNode(img.src, img.alt) };
        },
        priority: 0,
      }),
    };
  }

  exportJSON(): SerializedImageNode {
    return {
      src: this.__src,
      altText: this.__altText,
      type: 'image',
      version: 1,
    };
  }

  static importJSON(serializedNode: SerializedImageNode): ImageNode {
    return new ImageNode(serializedNode.src, serializedNode.altText);
  }

  decorate(): React.JSX.Element {
    return (
      <img
        src={this.__src}
        alt={this.__altText}
        className="rounded-md max-w-full h-auto shadow-sm"
      />
    );
  }
}

const IMAGE_TRANSFORMER = {
  dependencies: [ImageNode],
  export: (node: LexicalNode) => {
    if (!(node instanceof ImageNode)) return null;
    return `![${node.__altText}](${node.__src})`;
  },
  regExp: /!\[([^\]]*)\]\(([^)]+)\)/,
  replace: (textNode: any, match: RegExpMatchArray) => {
    const [, altText, src] = match;
    const imageNode = new ImageNode(src, altText);
    textNode.replace(imageNode);
  },
  type: 'text-match' as const,
};

const NOTEBOOK_TRANSFORMERS = [
  ...TRANSFORMERS,
  MARKDOWN_TABLE_TRANSFORMER,
  INTERNAL_LINK_TRANSFORMER,
  IMAGE_TRANSFORMER,
];

// ============================================================================
// Editor Theme & Nodes
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
  table: 'border-collapse border border-border w-full my-4 table-auto',
  tableRow: 'border-b border-border',
  tableCell: 'border border-border px-3 py-2 min-w-[80px] align-top',
  tableCellHeader: 'border border-border px-3 py-2 min-w-[80px] font-semibold bg-muted/50 text-left',
  tableCellSelected: 'bg-primary/10',
  text: {
    bold: 'font-bold',
    italic: 'italic',
    underline: 'underline',
    strikethrough: 'line-through',
    code: 'bg-muted/80 px-1.5 py-0.5 rounded font-mono text-sm text-primary',
  },
};

const editorNodes = [
  HeadingNode,
  QuoteNode,
  ListNode,
  ListItemNode,
  CodeNode,
  CodeHighlightNode,
  LinkNode,
  AutoLinkNode,
  TableNode,
  TableRowNode,
  TableCellNode,
  ImageNode,
];

// ============================================================================
// Helper Functions
// ============================================================================

function isNodeInsideTable(node: LexicalNode): boolean {
  let current: LexicalNode | null = node;
  while (current) {
    if ($isTableNode(current)) return true;
    current = current.getParent();
  }
  return false;
}

function createClipboardEditor(): LexicalEditor {
  return createEditor({
    namespace: 'NotebookClipboard',
    nodes: editorNodes,
    onError: (error) => console.error('[NotebookClipboard Error]:', error),
  });
}

function markdownToHtml(markdown: string): string {
  const clipboardEditor = createClipboardEditor();
  clipboardEditor.update(
    () => {
      $convertFromMarkdownString(markdown, NOTEBOOK_TRANSFORMERS);
    },
    { discrete: true }
  );

  let html = '';
  clipboardEditor.getEditorState().read(() => {
    html = $generateHtmlFromNodes(clipboardEditor, null);
  });
  return html;
}

function htmlToMarkdown(html: string): string {
  const clipboardEditor = createClipboardEditor();
  const parser = new DOMParser();
  clipboardEditor.update(
    () => {
      const root = $getRoot();
      const dom = parser.parseFromString(html, 'text/html');
      const nodes = $generateNodesFromDOM(clipboardEditor, dom);
      root.clear();
      root.append(...nodes);
    },
    { discrete: true }
  );

  let markdown = '';
  clipboardEditor.getEditorState().read(() => {
    markdown = $convertToMarkdownString(NOTEBOOK_TRANSFORMERS);
  });
  return markdown;
}

function isMarkdownContent(text: string): boolean {
  const patterns = [
    /^#{1,6}\s+.+/m,
    /^\s*[-*+]\s+.+/m,
    /^\s*\d+[.)]\s+.+/m,
    /^\s*>\s+.+/m,
    /^\s*```/m,
    /^\s*~~~\s*/m,
    /^\s*(---|\*\*\*|___)\s*$/m,
    /\*\*[^*]+\*\*/,
    /(?<!\*)\*[^*\n]+\*(?!\*)/,
    /(?<!_)__[^_]+__(?!_)/,
    /~~[^~]+~~/,
    /`[^`\n]+`/,
    /\[[^\]]+\]\([^)]+\)/,
    /^\|.+\|$/m,
  ];
  return patterns.some((pattern) => pattern.test(text));
}

function isMarkdownTableContent(content: string): boolean {
  const lines = content.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (lines.length < 2) return false;
  for (let index = 0; index < lines.length - 1; index += 1) {
    if (
      MARKDOWN_TABLE_ROW_REGEX.test(lines[index]) &&
      MARKDOWN_TABLE_DIVIDER_REGEX.test(lines[index + 1])
    ) {
      return true;
    }
  }
  return false;
}

function compressImage(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.readAsDataURL(file);
    reader.onload = (event) => {
      const img = new Image();
      img.src = event.target?.result as string;
      img.onload = () => {
        const canvas = document.createElement('canvas');
        const MAX_WIDTH = 1200;
        const MAX_HEIGHT = 1200;
        let width = img.width;
        let height = img.height;

        if (width > height) {
          if (width > MAX_WIDTH) {
            height *= MAX_WIDTH / width;
            width = MAX_WIDTH;
          }
        } else {
          if (height > MAX_HEIGHT) {
            width *= MAX_HEIGHT / height;
            height = MAX_HEIGHT;
          }
        }

        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext('2d');
        ctx?.drawImage(img, 0, 0, width, height);
        resolve(canvas.toDataURL('image/jpeg', 0.8));
      };
      img.onerror = reject;
    };
    reader.onerror = reject;
  });
}

// ============================================================================
// Custom UI Components
// ============================================================================

interface ImageUrlModalProps {
  onClose: () => void;
  onInsert: (url: string) => void;
}

function ImageUrlModal({ onClose, onInsert }: ImageUrlModalProps) {
  const [url, setUrl] = useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!url.trim()) return;
    onInsert(url.trim());
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-[100] p-4">
      <div className="bg-card border border-border rounded-xl p-5 max-w-md w-full shadow-xl">
        <h3 className="text-lg font-semibold mb-4 flex items-center gap-2">
          <ImageIcon size={18} />
          Insert Image from URL
        </h3>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium mb-1">Image URL</label>
            <input
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              className="w-full px-3 py-2 bg-background border border-border rounded-md
                         focus:outline-none focus:ring-2 focus:ring-primary/50"
              placeholder="https://example.com/image.jpg"
              autoFocus
              required
            />
          </div>
          <div className="flex gap-2 pt-2">
            <Button type="submit" className="flex-1" variant="save">
              Insert
            </Button>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ============================================================================
// Plugins
// ============================================================================

function MarkdownClipboardPlugin() {
  const [editor] = useLexicalComposerContext();

  useEffect(() => {
    return mergeRegister(
      editor.registerCommand(
        COPY_COMMAND,
        (event) => {
          if (!event || !('clipboardData' in event) || !event.clipboardData) return false;
          const selection = $getSelection();
          if (!selection || selection.isCollapsed() || !$isRangeSelection(selection)) return false;

          const selectionContainsTable = selection.getNodes().some(isNodeInsideTable);
          if (selectionContainsTable) return false;

          const clipboardData = event.clipboardData;
          const html = $generateHtmlFromNodes(editor, selection);
          const markdown = html ? htmlToMarkdown(html) : selection.getTextContent();

          clipboardData.setData('text/plain', markdown);
          if (html) clipboardData.setData('text/html', html);
          event.preventDefault();
          return true;
        },
        COMMAND_PRIORITY_HIGH
      ),
      editor.registerCommand(
        PASTE_COMMAND,
        (event) => {
          if (!event || !('clipboardData' in event) || !event.clipboardData) return false;
          const clipboardData = event.clipboardData;
          const selection = $getSelection();
          if (!selection || !$isRangeSelection(selection)) return false;

          const html = clipboardData.getData('text/html');
          const plainText = clipboardData.getData('text/plain');
          const lexicalPayload = clipboardData.getData('application/x-lexical-editor');

          if (!html && !plainText && !lexicalPayload) return false;
          if (lexicalPayload && lexicalPayload.trim().length > 0) return false;
          if (selection.getNodes().some(isNodeInsideTable)) return false;

          const plainTextIsMarkdownTable = isMarkdownTableContent(plainText);
          const plainTextIsMarkdown = isMarkdownContent(plainText);
          const htmlHasTable = /<table\b/i.test(html);

          if (!html.trim() && !plainTextIsMarkdownTable && !plainTextIsMarkdown) return false;

          event.preventDefault();
          let nodes: LexicalNode[] = [];

          try {
            if (plainTextIsMarkdownTable && !htmlHasTable) {
              const convertedHtml = markdownToHtml(plainText);
              if (convertedHtml) {
                const dom = new DOMParser().parseFromString(convertedHtml, 'text/html');
                nodes = $generateNodesFromDOM(editor, dom);
              }
            } else if (html.trim()) {
              const dom = new DOMParser().parseFromString(html, 'text/html');
              nodes = $generateNodesFromDOM(editor, dom);
            } else if (plainTextIsMarkdown) {
              const convertedHtml = markdownToHtml(plainText);
              if (convertedHtml) {
                const dom = new DOMParser().parseFromString(convertedHtml, 'text/html');
                nodes = $generateNodesFromDOM(editor, dom);
              }
            }
          } catch (err) {
            console.error('[NotebookPaste] failed to parse clipboard content:', err);
          }

          if (nodes.length === 0) {
            selection.insertText(plainText);
            return true;
          }
          $insertNodes(nodes);
          return true;
        },
        COMMAND_PRIORITY_HIGH
      )
    );
  }, [editor]);

  return null;
}

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
  const [showTableMenu, setShowTableMenu] = useState(false);
  const [tableRows, setTableRows] = useState('3');
  const [tableColumns, setTableColumns] = useState('3');
  const [includeHeaderRow, setIncludeHeaderRow] = useState(true);
  const [showImageModal, setShowImageModal] = useState(false);
  const tableSelectionRef = React.useRef<ReturnType<typeof $getSelection>>(null);

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
        anchorNode.getKey() === 'root' ? anchorNode : anchorNode.getTopLevelElementOrThrow();
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
        editorState.read(() => updateToolbar());
      }),
      editor.registerCommand(CAN_UNDO_COMMAND, (payload) => { setCanUndo(payload); return false; }, COMMAND_PRIORITY_CRITICAL),
      editor.registerCommand(CAN_REDO_COMMAND, (payload) => { setCanRedo(payload); return false; }, COMMAND_PRIORITY_CRITICAL)
    );
  }, [editor, updateToolbar]);

  const formatParagraph = () => {
    editor.update(() => {
      const selection = $getSelection();
      if ($isRangeSelection(selection)) $setBlocksType(selection, () => $createParagraphNode());
    });
  };

  const formatHeading = (headingSize: HeadingTagType) => {
    if (blockType !== headingSize) {
      editor.update(() => {
        const selection = $getSelection();
        if ($isRangeSelection(selection)) $setBlocksType(selection, () => $createHeadingNode(headingSize));
      });
    } else {
      formatParagraph();
    }
  };

  const formatQuote = () => {
    if (blockType !== 'quote') {
      editor.update(() => {
        const selection = $getSelection();
        if ($isRangeSelection(selection)) $setBlocksType(selection, () => $createQuoteNode());
      });
    } else {
      formatParagraph();
    }
  };

  const formatCodeBlock = () => {
    if (blockType !== 'code') {
      editor.update(() => {
        const selection = $getSelection();
        if ($isRangeSelection(selection)) $setBlocksType(selection, () => $createCodeNode());
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

  const insertMath = () => {
    editor.update(() => {
      const selection = $getSelection();
      if ($isRangeSelection(selection)) {
        const p = $createParagraphNode();
        p.append($createTextNode('$$ E = mc^2 $$'));
        selection.insertNodes([p]);
      }
    });
  };

  const insertMermaid = () => {
    editor.update(() => {
      const selection = $getSelection();
      if ($isRangeSelection(selection)) {
        const codeNode = $createCodeNode('mermaid');
        codeNode.append($createTextNode('graph TD;\n    A-->B;'));
        const emptyParagraph = $createParagraphNode();
        selection.insertNodes([codeNode, emptyParagraph]);
        emptyParagraph.select();
      }
    });
  };

  const insertChart = () => {
    editor.update(() => {
      const selection = $getSelection();
      if ($isRangeSelection(selection)) {
        const codeNode = $createCodeNode('chart');
        const template = `{\n  "type": "bar",\n  "data": [\n    { "label": "A", "value": 10 },\n    { "label": "B", "value": 20 }\n  ]\n}`;
        codeNode.append($createTextNode(template));
        const emptyParagraph = $createParagraphNode();
        selection.insertNodes([codeNode, emptyParagraph]);
        emptyParagraph.select();
      }
    });
  };

  const handleInsertImageUrl = (url: string) => {
    editor.update(() => {
      const selection = $getSelection();
      if ($isRangeSelection(selection)) {
        const node = new ImageNode(url, 'Image');
        const emptyParagraph = $createParagraphNode();
        selection.insertNodes([node, emptyParagraph]);
        emptyParagraph.select();
      }
    });
    setShowImageModal(false);
  };

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    try {
      const compressedBase64 = await compressImage(file);
      editor.update(() => {
        const selection = $getSelection();
        if ($isRangeSelection(selection)) {
          const node = new ImageNode(compressedBase64, file.name);
          const emptyParagraph = $createParagraphNode();
          selection.insertNodes([node, emptyParagraph]);
          emptyParagraph.select();
        }
      });
    } catch (err) {
      console.error('Failed to process image:', err);
      alert('Failed to process image. Please try a different file.');
    }
    e.target.value = '';
  };

  const insertGallery = () => {
    editor.update(() => {
      const selection = $getSelection();
      if ($isRangeSelection(selection)) {
        const codeNode = $createCodeNode('image_gallery');
        const template = `{\n  "images": [\n    { "url": "https://example.com/img1.jpg", "caption": "Image 1" },\n    { "url": "https://example.com/img2.jpg", "caption": "Image 2" }\n  ]\n}`;
        codeNode.append($createTextNode(template));
        const emptyParagraph = $createParagraphNode();
        selection.insertNodes([codeNode, emptyParagraph]);
        emptyParagraph.select();
      }
    });
  };

  const openTableMenu = () => {
    editor.getEditorState().read(() => {
      tableSelectionRef.current = $getSelection()?.clone() ?? null;
    });
    setShowTableMenu(true);
  };

  const insertTable = () => {
    const rows = Number(tableRows);
    const columns = Number(tableColumns);

    if (!Number.isInteger(rows) || !Number.isInteger(columns) || rows < 1 || rows > 50 || columns < 1 || columns > 50) {
      window.alert('Rows and columns must be between 1 and 50.');
      return;
    }

    if (tableSelectionRef.current) {
      editor.update(() => $setSelection(tableSelectionRef.current!.clone()));
    }

    editor.dispatchCommand(INSERT_TABLE_COMMAND, {
      rows: String(rows),
      columns: String(columns),
      includeHeaders: includeHeaderRow,
    });

    tableSelectionRef.current = null;
    setShowTableMenu(false);
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
        active ? 'bg-primary/15 text-primary font-semibold' : 'text-muted-foreground hover:text-foreground hover:bg-muted'
      } disabled:opacity-40 disabled:pointer-events-none`}
    >
      {children}
    </button>
  );

  return (
    <div className="flex flex-wrap items-center gap-1 px-3 py-2 border-b border-muted bg-gigbg sticky top-0 z-10 backdrop-blur-sm">
      <ToolbarButton disabled={!canUndo} onClick={() => editor.dispatchCommand(UNDO_COMMAND, undefined)} title="Undo">
        <Undo size={15} />
      </ToolbarButton>
      <ToolbarButton disabled={!canRedo} onClick={() => editor.dispatchCommand(REDO_COMMAND, undefined)} title="Redo">
        <Redo size={15} />
      </ToolbarButton>
      <div className="w-[1px] h-5 bg-border mx-1" />
      <ToolbarButton active={blockType === 'paragraph'} onClick={formatParagraph} title="Paragraph">
        <Pilcrow size={15} />
      </ToolbarButton>
      <ToolbarButton active={blockType === 'h1'} onClick={() => formatHeading('h1')} title="Heading 1">
        <Heading1 size={15} />
      </ToolbarButton>
      <ToolbarButton active={blockType === 'h2'} onClick={() => formatHeading('h2')} title="Heading 2">
        <Heading2 size={15} />
      </ToolbarButton>
      <ToolbarButton active={blockType === 'h3'} onClick={() => formatHeading('h3')} title="Heading 3">
        <Heading3 size={15} />
      </ToolbarButton>
      <div className="w-[1px] h-5 bg-border mx-1" />
      <ToolbarButton active={isBold} onClick={() => editor.dispatchCommand(FORMAT_TEXT_COMMAND, 'bold')} title="Bold">
        <Bold size={15} />
      </ToolbarButton>
      <ToolbarButton active={isItalic} onClick={() => editor.dispatchCommand(FORMAT_TEXT_COMMAND, 'italic')} title="Italic">
        <Italic size={15} />
      </ToolbarButton>
      <ToolbarButton active={isUnderline} onClick={() => editor.dispatchCommand(FORMAT_TEXT_COMMAND, 'underline')} title="Underline">
        <Underline size={15} />
      </ToolbarButton>
      <ToolbarButton active={isStrikethrough} onClick={() => editor.dispatchCommand(FORMAT_TEXT_COMMAND, 'strikethrough')} title="Strikethrough">
        <Strikethrough size={15} />
      </ToolbarButton>
      <ToolbarButton active={isCode} onClick={() => editor.dispatchCommand(FORMAT_TEXT_COMMAND, 'code')} title="Inline Code">
        <Code size={15} />
      </ToolbarButton>
      <div className="w-[1px] h-5 bg-border mx-1" />
      <ToolbarButton active={blockType === 'ul'} onClick={() => editor.dispatchCommand(INSERT_UNORDERED_LIST_COMMAND, undefined)} title="Bullet List">
        <List size={15} />
      </ToolbarButton>
      <ToolbarButton active={blockType === 'ol'} onClick={() => editor.dispatchCommand(INSERT_ORDERED_LIST_COMMAND, undefined)} title="Numbered List">
        <ListOrdered size={15} />
      </ToolbarButton>
      <ToolbarButton active={blockType === 'quote'} onClick={formatQuote} title="Quote">
        <Quote size={15} />
      </ToolbarButton>
      <ToolbarButton active={blockType === 'code'} onClick={formatCodeBlock} title="Code Block">
        <FileCode size={15} />
      </ToolbarButton>
      <ToolbarButton onClick={insertLink} title="Insert Link">
        <Link2 size={15} />
      </ToolbarButton>
      <ToolbarButton onClick={insertMath} title="Math Equation">
        <Sigma size={15} />
      </ToolbarButton>
      <ToolbarButton onClick={insertMermaid} title="Mermaid Diagram">
        <Network size={15} />
      </ToolbarButton>
      <ToolbarButton onClick={insertChart} title="Recharts Chart">
        <BarChart3 size={15} />
      </ToolbarButton>
      <div className="w-[1px] h-5 bg-border mx-1" />
      <ToolbarButton onClick={() => setShowImageModal(true)} title="Insert Image from URL">
        <ImageIcon size={15} />
      </ToolbarButton>
      <label
        className="p-1.5 rounded-md transition-colors flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-muted cursor-pointer"
        title="Upload Image from Device"
      >
        <Upload size={15} />
        <input type="file" accept="image/*" className="hidden" onChange={handleFileUpload} />
      </label>
      <ToolbarButton onClick={insertGallery} title="Insert Image Gallery">
        <Images size={15} />
      </ToolbarButton>
      <div className="relative">
        <ToolbarButton onClick={openTableMenu} title="Insert Table">
          <Table2 size={15} />
          <span className="text-xs font-semibold">Table</span>
        </ToolbarButton>

        {showTableMenu && (
          <div className="absolute right-0 top-full z-50 mt-2 w-64 rounded-lg border border-border bg-card p-3 shadow-xl">
            <div className="grid grid-cols-2 gap-3">
              <label className="text-xs font-medium text-muted-foreground">
                Rows
                <input
                  type="number"
                  min="1"
                  max="50"
                  step="1"
                  value={tableRows}
                  onChange={(event) => setTableRows(event.target.value)}
                  className="mt-1 w-full rounded-md border border-border bg-background px-2 py-2 text-sm text-foreground"
                />
              </label>
              <label className="text-xs font-medium text-muted-foreground">
                Columns
                <input
                  type="number"
                  min="1"
                  max="50"
                  step="1"
                  value={tableColumns}
                  onChange={(event) => setTableColumns(event.target.value)}
                  className="mt-1 w-full rounded-md border border-border bg-background px-2 py-2 text-sm text-foreground"
                />
              </label>
            </div>
            <label className="mt-3 flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={includeHeaderRow}
                onChange={(event) => setIncludeHeaderRow(event.target.checked)}
                className="accent-primary"
              />
              Include header row
            </label>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setShowTableMenu(false)}
                className="rounded-md px-3 py-2 text-xs text-muted-foreground hover:bg-muted"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={insertTable}
                className="rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground hover:opacity-90"
              >
                Insert Table
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Image Modal placed inside ToolbarPlugin to access its state */}
      {showImageModal && (
        <ImageUrlModal onClose={() => setShowImageModal(false)} onInsert={handleInsertImageUrl} />
      )}
    </div>
  );
}

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

function CodeHighlightPluginWrapper() {
  const [editor] = useLexicalComposerContext();
  useEffect(() => {
    return registerCodeHighlighting(editor);
  }, [editor]);
  return null;
}

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
  const [showLivePreview, setShowLivePreview] = useState(false);
  const [liveMarkdown, setLiveMarkdown] = useState(initialContent);

  const initialConfig = useMemo(
    () => ({
      namespace: 'NotebookEditor',
      nodes: editorNodes,
      theme: editorTheme,
      onError: (error: Error) => console.error('[NotebookEditor Error]:', error),
      editable: !readOnly,
    }),
    [readOnly]
  );

  const previewTimerRef = React.useRef<NodeJS.Timeout | null>(null);

  const handleChange = useCallback((editorState: EditorState) => {
    if (previewTimerRef.current) clearTimeout(previewTimerRef.current);
    previewTimerRef.current = setTimeout(() => {
      editorState.read(() => {
        const markdown = $convertToMarkdownString(NOTEBOOK_TRANSFORMERS);
        setLiveMarkdown(markdown);
      });
    }, 300);
  }, []);

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
      if (onSave) onSave(markdownContent);
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
    activeEditor = editor;
  }, []);

  return (
    <div className="flex flex-col h-full p-1 bg-rb/5">
      <div className="flex-1 flex flex-col max-w-7xl w-full mx-auto bg-card border border-border rounded-sm shadow-sm overflow-scroll focus-within:ring-1
       [&::-webkit-scrollbar]:w-0 focus-within:ring-card focus-within:border-card transition-all">
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
          <div className="flex-1 flex overflow-hidden min-h-0">
            <div className="relative flex-1 overflow-y-auto scrollbar-thin scrollbar-thumb-slate-500 scrollbar-track-slate-100
             [&::-webkit-scrollbar]:w-1 [&::-webkit-scrollbar-track]:bg-card [&::-webkit-scrollbar-thumb]:bg-muted [&::-webkit-scrollbar-thumb]:rounded-full">
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

            {showLivePreview && (
              <div className="w-1/2 border-l border-border overflow-y-auto p-8 bg-card">
                <MarkdownRenderer content={liveMarkdown} />
              </div>
            )}
          </div>
          <HistoryPlugin />
          <OnChangePlugin onChange={handleChange} />

          {!readOnly && <MarkdownClipboardPlugin />}

          <MarkdownShortcutPlugin transformers={NOTEBOOK_TRANSFORMERS} />
          <LinkPlugin />
          <ListPlugin />
          <TablePlugin />
          <CodeHighlightPluginWrapper />
          <SlashCommandsPlugin />
        </LexicalComposer>
        {!readOnly && (
          <div className="border-t border-border px-4 py-2.5 flex items-center justify-between bg-muted/20">
            <div className="flex items-center gap-4">
              <button
                onClick={() => setShowLivePreview(!showLivePreview)}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium bg-muted border border-border rounded-md hover:bg-muted/80 transition-colors"
                title="Toggle Live Preview"
              >
                {showLivePreview ? <PanelRightClose size={14} /> : <PanelRightOpen size={14} />}
                {showLivePreview ? 'Hide Preview' : 'Live Preview'}
              </button>
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
