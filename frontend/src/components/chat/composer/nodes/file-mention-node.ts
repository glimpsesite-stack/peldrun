// frontend/src/components/chat/composer/nodes/file-mention-node.ts
/**
 * FileMentionNode — a Lexical TextNode subclass that represents an inline
 * clickable reference to a workspace or application-root file.
 *
 * The node stores both the canonical relative path and its origin. A
 * `workspace` reference is resolved inside the active chat/project workspace;
 * a `global` reference is resolved inside the Peldrun application root.
 */

import {
  $applyNodeReplacement,
  DOMConversionMap,
  EditorConfig,
  LexicalNode,
  NodeKey,
  SerializedTextNode,
  Spread,
  TextNode,
} from "lexical";

export type FileMentionOrigin = "workspace" | "global";

export type SerializedFileMentionNode = Spread<
  {
    filePath: string;
    /** Older serialized nodes omit this and are treated as workspace refs. */
    fileOrigin?: FileMentionOrigin;
  },
  SerializedTextNode
>;

export class FileMentionNode extends TextNode {
  __filePath: string;
  __fileOrigin: FileMentionOrigin;

  static getType(): string {
    return "file-mention";
  }

  static clone(node: FileMentionNode): FileMentionNode {
    return new FileMentionNode(
      node.__text,
      node.__filePath,
      node.__fileOrigin,
      node.__key
    );
  }

  constructor(
    text: string,
    filePath: string,
    fileOrigin: FileMentionOrigin = "workspace",
    key?: NodeKey
  ) {
    super(text, key);
    this.__filePath = filePath;
    this.__fileOrigin = fileOrigin;
  }

  /** Returns the canonical relative path of the referenced file. */
  getFilePath(): string {
    return this.getLatest().__filePath;
  }

  /** Identifies which safe root the backend should use for this path. */
  getFileOrigin(): FileMentionOrigin {
    return this.getLatest().__fileOrigin;
  }

  /** Human-readable display text (without the `@[...]` wrapper). */
  getDisplayName(): string {
    const text = this.getTextContent();
    const m = text.match(/^@\[([^\]]+)\]$/);
    return m ? m[1] : text;
  }

  createDOM(config: EditorConfig): HTMLElement {
    const dom = super.createDOM(config);
    dom.classList.add("peldrun-file-mention");
    dom.dataset.filePath = this.__filePath;
    dom.dataset.fileOrigin = this.__fileOrigin;
    dom.dataset.fileName = this.getDisplayName();
    dom.setAttribute(
      "title",
      this.__fileOrigin === "global"
        ? `Application-root reference: ${this.__filePath} (included as context)`
        : `Click to open "${this.__filePath}" in the workspace panel`
    );
    // Inline styles keep the node styled regardless of Tailwind purge.
    dom.style.color = "#2563eb";
    dom.style.textDecoration = "underline";
    dom.style.textUnderlineOffset = "2px";
    dom.style.cursor = this.__fileOrigin === "global" ? "default" : "pointer";
    dom.style.fontWeight = "500";
    return dom;
  }

  updateDOM(
    prevNode: this,
    dom: HTMLElement,
    config: EditorConfig
  ): boolean {
    const updated = super.updateDOM(prevNode, dom, config);
    if (prevNode.getFilePath() !== this.getFilePath()) {
      const newPath = this.getFilePath();
      dom.dataset.filePath = newPath;
      dom.setAttribute(
        "title",
        this.__fileOrigin === "global"
          ? `Application-root reference: ${newPath} (included as context)`
          : `Click to open "${newPath}" in the workspace panel`
      );
    }
    if (prevNode.getFileOrigin() !== this.getFileOrigin()) {
      dom.dataset.fileOrigin = this.getFileOrigin();
      dom.style.cursor = this.getFileOrigin() === "global" ? "default" : "pointer";
      dom.setAttribute(
        "title",
        this.getFileOrigin() === "global"
          ? `Application-root reference: ${this.getFilePath()} (included as context)`
          : `Click to open "${this.getFilePath()}" in the workspace panel`
      );
    }
    return updated;
  }

  static importDOM(): DOMConversionMap | null {
    return null;
  }

  static importJSON(serialized: SerializedFileMentionNode): FileMentionNode {
    return $createFileMentionNode(
      serialized.text,
      serialized.filePath,
      serialized.fileOrigin ?? "workspace"
    );
  }

  exportJSON(): SerializedFileMentionNode {
    return {
      ...super.exportJSON(),
      filePath: this.__filePath,
      fileOrigin: this.__fileOrigin,
      type: "file-mention",
      version: 1,
    };
  }

  /** Treat the mention as an atomic unit for cursor / deletion purposes. */
  isTextEntity(): true {
    return true;
  }

  /** Prevent accidental in-place editing of the mention text. */
  canInsertTextBefore(): false {
    return false;
  }

  canInsertTextAfter(): false {
    return false;
  }
}

export function $createFileMentionNode(
  text: string,
  filePath: string,
  fileOrigin: FileMentionOrigin = "workspace"
): FileMentionNode {
  return $applyNodeReplacement(
    new FileMentionNode(text, filePath, fileOrigin)
  );
}

export function $isFileMentionNode(
  node: LexicalNode | null | undefined
): node is FileMentionNode {
  return node instanceof FileMentionNode;
}

/**
 * Extract the display-worthy portion of a canonical workspace-relative path.
 * Strips storage prefixes used by the chat-scoped upload endpoints.
 */
export function getDisplayPathFromRelPath(relPath: string): string {
  if (!relPath) return relPath;
  let clean = relPath.replace(/\\/g, "/");
  if (clean.includes("/files/")) {
    clean = clean.split("/files/").pop() || clean;
  } else if (clean.startsWith("workspace/")) {
    clean = clean.slice("workspace/".length);
  }
  return clean;
}

/**
 * Inject a one-time stylesheet for the paperclip glyph. The node is styled
 * here rather than through Tailwind because Lexical creates its DOM directly.
 */
if (
  typeof document !== "undefined" &&
  !document.getElementById("peldrun-file-mention-styles")
) {
  const styleEl = document.createElement("style");
  styleEl.id = "peldrun-file-mention-styles";
  styleEl.textContent = `
    .peldrun-file-mention {
      display: inline-flex;
      align-items: center;
      gap: 3px;
      padding: 0 4px;
      border-radius: 4px;
      background: rgba(37, 99, 235, 0.08);
      transition: background-color 120ms ease;
    }
    .peldrun-file-mention:hover {
      background: rgba(37, 99, 235, 0.15);
    }
    .peldrun-file-mention::before {
      content: "";
      display: inline-block;
      width: 12px;
      height: 12px;
      flex-shrink: 0;
      background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%232563eb' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z'/><polyline points='14 2 14 8 20 8'/></svg>");
      background-size: contain;
      background-repeat: no-repeat;
      background-position: center;
    }
  `;
  document.head.appendChild(styleEl);
}
