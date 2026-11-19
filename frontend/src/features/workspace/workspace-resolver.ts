// frontend/src/features/workspace/workspace-resolver.ts
import { useState, useCallback, useMemo } from "react";
import { workspaceApi } from "./workspace-api";
import {
  WorkspaceFileReadResponse,
  GitRepositoryInfo,
  ArtifactMetadataResponse,
} from "./types";

export type WorkspaceTab = "preview" | "files" | "edit" | "artifacts" | "log";

export interface WorkspaceState {
  activeTab: WorkspaceTab;
  selectedFile: string | null;
  files: string[];
  currentFileContent: WorkspaceFileReadResponse | null;
  repositories: GitRepositoryInfo[];
  selectedRepository: GitRepositoryInfo | null;
  artifacts: ArtifactMetadataResponse[];
  isLoading: boolean;
  error: string | null;
}

export interface WorkspaceActions {
  setActiveTab: (tab: WorkspaceTab) => void;
  selectFile: (path: string) => void;
  loadFiles: (path?: string) => Promise<void>;
  loadRepositories: () => Promise<void>;
  selectRepository: (repo: GitRepositoryInfo) => void;
  loadArtifacts: (sessionId?: string) => Promise<void>;
  refresh: () => Promise<void>;
}

export function useWorkspaceResolver(
  projectId: string,
  initialTab: WorkspaceTab = "files"
): [WorkspaceState, WorkspaceActions] {
  const [state, setState] = useState<WorkspaceState>({
    activeTab: initialTab,
    selectedFile: null,
    files: [],
    currentFileContent: null,
    repositories: [],
    selectedRepository: null,
    artifacts: [],
    isLoading: false,
    error: null,
  });

  const setActiveTab = useCallback((tab: WorkspaceTab) => {
    setState((prev) => ({ ...prev, activeTab: tab }));
  }, []);

  const selectFile = useCallback(async (path: string) => {
    setState((prev) => ({ ...prev, isLoading: true, error: null }));
    try {
      const fileContent = await workspaceApi.readFile(projectId, path);
      setState((prev) => ({
        ...prev,
        selectedFile: path,
        currentFileContent: fileContent,
        isLoading: false,
      }));
    } catch (err) {
      setState((prev) => ({
        ...prev,
        isLoading: false,
        error: err instanceof Error ? err.message : "Failed to load file",
      }));
    }
  }, [projectId]);

  const loadFiles = useCallback(async (path: string = "") => {
    setState((prev) => ({ ...prev, isLoading: true, error: null }));
    try {
      const files = await workspaceApi.listFiles(projectId, path);
      setState((prev) => ({ ...prev, files, isLoading: false }));
    } catch (err) {
      setState((prev) => ({
        ...prev,
        isLoading: false,
        error: err instanceof Error ? err.message : "Failed to load files",
      }));
    }
  }, [projectId]);

  const loadRepositories = useCallback(async () => {
    setState((prev) => ({ ...prev, isLoading: true, error: null }));
    try {
      const repositories = await workspaceApi.discoverRepositories(projectId);
      setState((prev) => ({ ...prev, repositories, isLoading: false }));
    } catch (err) {
      setState((prev) => ({
        ...prev,
        isLoading: false,
        error: err instanceof Error ? err.message : "Failed to load repositories",
      }));
    }
  }, [projectId]);

  const selectRepository = useCallback((repo: GitRepositoryInfo) => {
    setState((prev) => ({ ...prev, selectedRepository: repo }));
  }, []);

  const loadArtifacts = useCallback(async (sessionId?: string) => {
    setState((prev) => ({ ...prev, isLoading: true, error: null }));
    try {
      const artifacts = await workspaceApi.getArtifacts(projectId, sessionId);
      setState((prev) => ({ ...prev, artifacts, isLoading: false }));
    } catch (err) {
      setState((prev) => ({
        ...prev,
        isLoading: false,
        error: err instanceof Error ? err.message : "Failed to load artifacts",
      }));
    }
  }, [projectId]);

  const refresh = useCallback(async () => {
    setState((prev) => ({ ...prev, isLoading: true, error: null }));
    try {
      await Promise.all([loadFiles(), loadRepositories(), loadArtifacts()]);
      setState((prev) => ({ ...prev, isLoading: false }));
    } catch (err) {
      setState((prev) => ({
        ...prev,
        isLoading: false,
        error: err instanceof Error ? err.message : "Failed to refresh workspace",
      }));
    }
  }, [loadFiles, loadRepositories, loadArtifacts]);


  const actions = useMemo<WorkspaceActions>(
    () => ({
      setActiveTab,
      selectFile,
      loadFiles,
      loadRepositories,
      selectRepository,
      loadArtifacts,
      refresh,
    }),
    [setActiveTab, selectFile, loadFiles, loadRepositories, selectRepository, loadArtifacts, refresh]
  );

  return [state, actions];
}

export function validateFilePath(filePath: string): { isValid: boolean; error?: string } {
  if (filePath.includes("..")) return { isValid: false, error: "Path traversal not allowed" };
  if (filePath.startsWith("/") || filePath.startsWith("\\")) return { isValid: false, error: "Absolute paths not allowed" };
  if (filePath.includes("\0")) return { isValid: false, error: "Invalid characters in path" };
  if (!filePath || filePath.trim() === "") return { isValid: false, error: "File path cannot be empty" };
  return { isValid: true };
}

export function getFileExtension(filePath: string): string {
  const lastDot = filePath.lastIndexOf(".");
  if (lastDot === -1) return "";
  return filePath.substring(lastDot + 1).toLowerCase();
}

export function isEditableFile(filePath: string): boolean {
  const editableExtensions = ["txt", "md", "json", "js", "jsx", "ts", "tsx", "py", "html", "css", "scss", "sass", "xml", "yaml", "yml", "toml", "ini", "cfg", "conf", "sh", "bash", "zsh", "fish", "sql", "graphql", "gql"];
  return editableExtensions.includes(getFileExtension(filePath));
}

export function isPreviewableFile(filePath: string): boolean {
  const previewableExtensions = ["html", "htm", "svg", "md"];
  return previewableExtensions.includes(getFileExtension(filePath));
}
