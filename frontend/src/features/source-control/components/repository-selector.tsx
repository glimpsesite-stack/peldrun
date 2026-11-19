// frontend/src/features/source-control/components/repository-selector.tsx

import React, { useState, useEffect, useCallback, useRef } from "react";
import { FolderGit, RefreshCw, Loader2, GitBranch, CheckCircle2, AlertCircle } from "lucide-react";
import { workspaceApi } from "@/features/workspace/workspace-api";
import { GitRepositoryInfo } from "@/features/workspace/types";

export interface RepositorySelectorProps {
  projectId: string;
  selectedRepositoryId?: string | null;
  onSelect?: (repository: GitRepositoryInfo | null) => void;
  className?: string;
}

export const RepositorySelector: React.FC<RepositorySelectorProps> = ({
  projectId,
  selectedRepositoryId = null,
  onSelect,
  className = "",
}) => {
  const [repositories, setRepositories] = useState<GitRepositoryInfo[]>([]);
  const [selectedRepo, setSelectedRepo] = useState<GitRepositoryInfo | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Use a ref to hold the latest onSelect callback without adding it to useCallback dependencies
  const onSelectRef = useRef(onSelect);
  useEffect(() => {
    onSelectRef.current = onSelect;
  }, [onSelect]);

  const loadRepositories = useCallback(async () => {
    setIsLoading(true);
    setError(null);

    try {
      const repos = await workspaceApi.discoverRepositories(projectId);
      setRepositories(repos);

      if (selectedRepositoryId) {
        const existing = repos.find((r) => r.id === selectedRepositoryId);
        if (existing) {
          setSelectedRepo(existing);
        } else {
          setSelectedRepo(null);
          // Use the ref to call the latest onSelect without causing re-renders
          onSelectRef.current?.(null);
        }
      }
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Failed to load repositories";
      setError(errorMessage);
      console.error("[RepositorySelector] Failed to discover repositories:", err);
    } finally {
      setIsLoading(false);
    }
  }, [projectId, selectedRepositoryId]); // Removed onSelect from dependencies to prevent infinite loops

  // Initial load
  useEffect(() => {
    loadRepositories();
  }, [loadRepositories]);

  const handleSelect = (repoId: string) => {
    if (repoId === "") {
      setSelectedRepo(null);
      onSelectRef.current?.(null);
      return;
    }

    const repo = repositories.find((r) => r.id === repoId);
    if (repo) {
      setSelectedRepo(repo);
      onSelectRef.current?.(repo);
    }
  };

  const getStatusIcon = (status: string) => {
    switch (status) {
      case "clean":
        return <CheckCircle2 size={14} className="text-green-500" />;
      case "dirty":
        return <AlertCircle size={14} className="text-yellow-500" />;
      default:
        return <AlertCircle size={14} className="text-gray-400" />;
    }
  };

  const getStatusLabel = (status: string) => {
    switch (status) {
      case "clean":
        return "Clean";
      case "dirty":
        return "Uncommitted changes";
      default:
        return "Unknown";
    }
  };

  return (
    <div className={`repository-selector ${className}`}>
      <div className="flex items-center gap-2">
        <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
          <FolderGit size={14} />
          <span>Repository</span>
        </div>

        <div className="flex-1 relative">
          <select
            value={selectedRepo?.id || ""}
            onChange={(e) => handleSelect(e.target.value)}
            disabled={isLoading || repositories.length === 0}
            className="w-full h-8 px-2 pr-8 text-xs bg-background border border-border rounded-md
                       focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary
                       disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer
                       appearance-none"
            style={{
              backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23666' stroke-width='2'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E")`,
              backgroundRepeat: "no-repeat",
              backgroundPosition: "right 0.5rem center",
            }}
          >
            <option value="">
              {isLoading ? "Loading..." : repositories.length === 0 ? "No repositories found" : "Select a repository"}
            </option>
            {repositories.map((repo) => (
              <option key={repo.id} value={repo.id}>
                {repo.name} {repo.current_branch ? `(${repo.current_branch})` : ""}
              </option>
            ))}
          </select>
        </div>

        <button
          onClick={loadRepositories}
          disabled={isLoading}
          className="h-8 w-8 flex items-center justify-center rounded-md border border-border
                     bg-background hover:bg-muted transition-colors cursor-pointer
                     disabled:opacity-50 disabled:cursor-not-allowed"
          title="Refresh repositories"
        >
          {isLoading ? (
            <Loader2 size={14} className="animate-spin text-primary" />
          ) : (
            <RefreshCw size={14} className="text-muted-foreground" />
          )}
        </button>
      </div>

      {selectedRepo && (
        <div className="mt-2 p-2 bg-muted/50 border border-border rounded-md text-xs">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-1.5 min-w-0">
              <GitBranch size={12} className="text-muted-foreground shrink-0" />
              <span className="font-mono truncate">
                {selectedRepo.current_branch || "detached HEAD"}
              </span>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              {getStatusIcon(selectedRepo.git_status)}
              <span className="text-muted-foreground">
                {getStatusLabel(selectedRepo.git_status)}
              </span>
            </div>
          </div>
          <div className="mt-1 text-[10px] text-muted-foreground font-mono truncate" title={selectedRepo.root_path}>
            {selectedRepo.root_path}
          </div>
        </div>
      )}

      {error && (
        <div className="mt-2 p-2 bg-destructive/10 border border-destructive/30 rounded-md text-xs text-destructive">
          <span className="font-medium">Error:</span> {error}
        </div>
      )}

      {!isLoading && !error && repositories.length === 0 && (
        <div className="mt-2 p-2 bg-muted/30 border border-border rounded-md text-xs text-muted-foreground text-center">
          No Git repositories found in this project workspace.
          <br />
          <span className="text-[10px]">
            Initialize a repository with <code className="font-mono bg-background px-1 rounded">git init</code>
          </span>
        </div>
      )}
    </div>
  );
};

export default RepositorySelector;
