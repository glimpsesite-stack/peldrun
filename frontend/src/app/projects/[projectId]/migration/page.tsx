// frontend/src/app/projects/[projectId]/migration/page.tsx

/**
 * Project Migration Page - MigrationWizard
 *
 * Provides a safe, auditable interface for migrating session files
 * to the project workspace.
 *
 * Features:
 * - List all sessions available for migration
 * - Dry-run analysis before actual migration
 * - Conflict detection and resolution
 * - Safe execution with rollback capability
 * - Migration history and audit log
 * - Never deletes source files automatically
 *
 * Design Decisions:
 * - Uses workspaceApi for all migration operations
 * - Integrates with the backend MigrationEngine
 * - Provides clear visual feedback for conflicts
 * - Requires explicit user confirmation before execution
 * - Shows detailed operation logs for transparency
 */

"use client";

import React, { useState, useEffect, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import {
  FolderSync,
  Loader2,
  AlertCircle,
  CheckCircle2,
  XCircle,
  FileText,
  ArrowRight,
  RotateCcw,
  Play,
  Eye,
  History,
  X,
  ChevronDown,
  ChevronRight,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { workspaceApi } from "@/features/workspace/workspace-api";
import {
  MigrationManifest,
  MigrationOperation,
  MigrationConflict,
} from "@/features/workspace/types";
import { useSessionStore } from "@/stores/session-store";
import { parseChatDate } from "@/lib/chatsApi";

interface MigrationHistory {
  timestamp: string;
  sessionId: string;
  status: "success" | "partial" | "failed" | "rolled_back";
  operationsCount: number;
  conflictsCount: number;
}

export default function MigrationPage() {
  const params = useParams();
  const router = useRouter();
  const projectId = params.projectId as string;

  const { projectSessionsCache, loadProjectSessions } = useSessionStore();

  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [manifest, setManifest] = useState<MigrationManifest | null>(null);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [isExecuting, setIsExecuting] = useState(false);
  const [isRollingBack, setIsRollingBack] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [migrationHistory, setMigrationHistory] = useState<MigrationHistory[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const [expandedOperations, setExpandedOperations] = useState<Set<string>>(new Set());

  // Load project sessions on mount
  useEffect(() => {
    loadProjectSessions(projectId);
    loadMigrationHistory();
  }, [projectId, loadProjectSessions]);

  const loadMigrationHistory = async () => {
    // TODO: Implement migration history API
    // For now, use empty array
    setMigrationHistory([]);
  };

  const sessions = projectSessionsCache[projectId] || [];

  const handleDryRun = async () => {
    if (!selectedSessionId) {
      alert("Please select a session to migrate");
      return;
    }

    setIsAnalyzing(true);
    setError(null);
    setManifest(null);

    try {
      const result = await workspaceApi.dryRunMigration(projectId, selectedSessionId);
      setManifest(result);
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Failed to analyze migration";
      setError(errorMessage);
      console.error("[MigrationPage] Dry run failed:", err);
    } finally {
      setIsAnalyzing(false);
    }
  };

  const handleExecute = async () => {
    if (!selectedSessionId || !manifest) {
      alert("Please analyze migration first");
      return;
    }

    if (!confirm("Are you sure you want to execute this migration? Source files will NOT be deleted.")) {
      return;
    }

    setIsExecuting(true);
    setError(null);

    try {
      const result = await workspaceApi.executeMigration(projectId, selectedSessionId, manifest);
      setManifest(result);

      // Add to history
      const historyEntry: MigrationHistory = {
        timestamp: new Date().toISOString(),
        sessionId: selectedSessionId,
        status: result.conflicts.length > 0 ? "partial" : "success",
        operationsCount: result.operations.length,
        conflictsCount: result.conflicts.length,
      };
      setMigrationHistory((prev) => [historyEntry, ...prev]);

      alert("Migration completed successfully!");
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Failed to execute migration";
      setError(errorMessage);
      console.error("[MigrationPage] Execution failed:", err);
    } finally {
      setIsExecuting(false);
    }
  };

  const handleRollback = async () => {
    if (!manifest) {
      alert("No migration to rollback");
      return;
    }

    if (!confirm("Are you sure you want to rollback this migration? This will delete migrated files.")) {
      return;
    }

    setIsRollingBack(true);
    setError(null);

    try {
      const result = await workspaceApi.rollbackMigration(projectId, manifest);
      setManifest(result);

      // Add to history
      const historyEntry: MigrationHistory = {
        timestamp: new Date().toISOString(),
        sessionId: manifest.session_id,
        status: "rolled_back",
        operationsCount: result.operations.length,
        conflictsCount: result.conflicts.length,
      };
      setMigrationHistory((prev) => [historyEntry, ...prev]);

      alert("Rollback completed successfully!");
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Failed to rollback migration";
      setError(errorMessage);
      console.error("[MigrationPage] Rollback failed:", err);
    } finally {
      setIsRollingBack(false);
    }
  };

  const toggleOperationExpanded = (opIndex: string) => {
    setExpandedOperations((prev) => {
      const newSet = new Set(prev);
      if (newSet.has(opIndex)) {
        newSet.delete(opIndex);
      } else {
        newSet.add(opIndex);
      }
      return newSet;
    });
  };

  const getOperationStatusIcon = (status: string) => {
    switch (status) {
      case "success":
      case "conflict_resolved":
        return <CheckCircle2 size={14} className="text-emerald-500" />;
      case "skipped":
        return <XCircle size={14} className="text-slate-400" />;
      case "conflict":
        return <AlertCircle size={14} className="text-amber-500" />;
      case "failed":
      case "rollback_failed":
        return <XCircle size={14} className="text-rose-500" />;
      case "rolled_back":
        return <RotateCcw size={14} className="text-blue-500" />;
      default:
        return <FileText size={14} className="text-slate-400" />;
    }
  };

  const getOperationStatusColor = (status: string) => {
    switch (status) {
      case "success":
      case "conflict_resolved":
        return "bg-emerald-500/10 border-emerald-500/30 text-emerald-700 dark:text-emerald-400";
      case "skipped":
        return "bg-slate-500/10 border-slate-500/30 text-slate-700 dark:text-slate-400";
      case "conflict":
        return "bg-amber-500/10 border-amber-500/30 text-amber-700 dark:text-amber-400";
      case "failed":
      case "rollback_failed":
        return "bg-rose-500/10 border-rose-500/30 text-rose-700 dark:text-rose-400";
      case "rolled_back":
        return "bg-blue-500/10 border-blue-500/30 text-blue-700 dark:text-blue-400";
      default:
        return "bg-slate-500/10 border-slate-500/30 text-slate-700 dark:text-slate-400";
    }
  };

  const formatSessionDate = (dateVal?: string | number | null) => {
    const date = parseChatDate(dateVal);
    return date.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  };

  return (
    <div className="container mx-auto px-4 py-8 max-w-6xl">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <FolderSync className="text-primary" size={24} />
            Migrate Session Files
          </h1>
          <p className="text-muted-foreground mt-1">
            Safely migrate files from chat sessions to the project workspace.
          </p>
        </div>
        <Button
          variant="outline"
          onClick={() => setShowHistory(!showHistory)}
          className="gap-2"
        >
          <History size={16} />
          {showHistory ? "Hide" : "Show"} History
        </Button>
      </div>

      {/* Migration History */}
      {showHistory && (
        <div className="mb-8 bg-card border border-border rounded-lg p-6">
          <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
            <History size={18} />
            Migration History
          </h2>
          {migrationHistory.length === 0 ? (
            <p className="text-sm text-muted-foreground">No migration history yet.</p>
          ) : (
            <div className="space-y-2">
              {migrationHistory.map((entry, idx) => (
                <div
                  key={idx}
                  className="flex items-center justify-between p-3 bg-muted/30 border border-border rounded-md"
                >
                  <div className="flex items-center gap-3">
                    <div
                      className={`px-2 py-1 text-xs rounded border ${
                        entry.status === "success"
                          ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-700 dark:text-emerald-400"
                          : entry.status === "rolled_back"
                          ? "bg-blue-500/10 border-blue-500/30 text-blue-700 dark:text-blue-400"
                          : entry.status === "partial"
                          ? "bg-amber-500/10 border-amber-500/30 text-amber-700 dark:text-amber-400"
                          : "bg-rose-500/10 border-rose-500/30 text-rose-700 dark:text-rose-400"
                      }`}
                    >
                      {entry.status}
                    </div>
                    <div>
                      <div className="text-sm font-medium">Session: {entry.sessionId}</div>
                      <div className="text-xs text-muted-foreground">
                        {new Date(entry.timestamp).toLocaleString()}
                      </div>
                    </div>
                  </div>
                  <div className="text-sm text-muted-foreground">
                    {entry.operationsCount} operations, {entry.conflictsCount} conflicts
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Session Selection */}
      <div className="mb-8 bg-card border border-border rounded-lg p-6">
        <h2 className="text-lg font-semibold mb-4">Select Session to Migrate</h2>
        {sessions.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No sessions found in this project.
          </p>
        ) : (
          <div className="space-y-2">
            {sessions.map((session) => (
              <label
                key={session.id}
                className={`flex items-center gap-3 p-3 border rounded-md cursor-pointer transition-colors ${
                  selectedSessionId === session.id
                    ? "bg-primary/10 border-primary"
                    : "bg-background border-border hover:bg-muted/50"
                }`}
              >
                <input
                  type="radio"
                  name="session"
                  value={session.id}
                  checked={selectedSessionId === session.id}
                  onChange={(e) => setSelectedSessionId(e.target.value)}
                  className="w-4 h-4"
                />
                <div className="flex-1">
                  <div className="font-medium">{session.title || session.prompt || "Untitled"}</div>
                  <div className="text-xs text-muted-foreground">
                    Created: {formatSessionDate(session.created_at)}
                    {session.updated_at && session.updated_at !== session.created_at && (
                      <> · Updated: {formatSessionDate(session.updated_at)}</>
                    )}
                  </div>
                </div>
                <div className="text-xs text-muted-foreground">
                  ID: {session.id.substring(0, 8)}...
                </div>
              </label>
            ))}
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="mb-8 flex items-center gap-3">
        <Button
          onClick={handleDryRun}
          disabled={!selectedSessionId || isAnalyzing}
          className="gap-2"
        >
          {isAnalyzing ? (
            <Loader2 size={16} className="animate-spin" />
          ) : (
            <Eye size={16} />
          )}
          Analyze Migration (Dry Run)
        </Button>

        {manifest && (
          <>
            <Button
              onClick={handleExecute}
              disabled={isExecuting || isRollingBack}
              variant="rb"
              className="gap-2"
            >
              {isExecuting ? (
                <Loader2 size={16} className="animate-spin" />
              ) : (
                <Play size={16} />
              )}
              Execute Migration
            </Button>

            <Button
              onClick={handleRollback}
              disabled={isExecuting || isRollingBack}
              variant="outline"
              className="gap-2"
            >
              {isRollingBack ? (
                <Loader2 size={16} className="animate-spin" />
              ) : (
                <RotateCcw size={16} />
              )}
              Rollback
            </Button>
          </>
        )}
      </div>

      {/* Error Display */}
      {error && (
        <div className="mb-6 p-4 bg-destructive/10 border border-destructive/30 rounded-lg flex items-start gap-3">
          <AlertCircle className="text-destructive shrink-0 mt-0.5" size={18} />
          <div>
            <h3 className="font-semibold text-destructive mb-1">Migration Error</h3>
            <p className="text-sm text-muted-foreground">{error}</p>
          </div>
        </div>
      )}

      {/* Analysis Results */}
      {manifest && (
        <div className="space-y-6">
          {/* Summary */}
          <div className="bg-card border border-border rounded-lg p-6">
            <h2 className="text-lg font-semibold mb-4">Migration Analysis</h2>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div className="bg-muted/30 border border-border rounded-md p-3">
                <div className="text-2xl font-bold">
                  {manifest.operations.filter((op) => op.status === "pending").length}
                </div>
                <div className="text-xs text-muted-foreground">Ready to Migrate</div>
              </div>
              <div className="bg-muted/30 border border-border rounded-md p-3">
                <div className="text-2xl font-bold">
                  {manifest.operations.filter((op) => op.status === "skipped").length}
                </div>
                <div className="text-xs text-muted-foreground">Already Exists</div>
              </div>
              <div className="bg-muted/30 border border-border rounded-md p-3">
                <div className="text-2xl font-bold text-amber-600">
                  {manifest.conflicts.length}
                </div>
                <div className="text-xs text-muted-foreground">Conflicts</div>
              </div>
              <div className="bg-muted/30 border border-border rounded-md p-3">
                <div className="text-2xl font-bold">
                  {manifest.operations.length}
                </div>
                <div className="text-xs text-muted-foreground">Total Files</div>
              </div>
            </div>
          </div>

          {/* Conflicts */}
          {manifest.conflicts.length > 0 && (
            <div className="bg-amber-500/5 border border-amber-500/30 rounded-lg p-6">
              <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
                <AlertCircle size={20} className="text-amber-600" />
                Conflicts Detected
              </h2>
              <p className="text-sm text-muted-foreground mb-4">
                The following files have conflicts (different content in source and destination).
                They will be copied to a review folder for manual resolution.
              </p>
              <div className="space-y-2">
                {manifest.conflicts.map((conflict, idx) => (
                  <div
                    key={idx}
                    className="bg-background border border-border rounded-md p-3"
                  >
                    <div className="font-mono text-sm mb-1">{conflict.destination}</div>
                    <div className="text-xs text-muted-foreground">
                      Source: {conflict.source}
                    </div>
                    <div className="text-xs text-muted-foreground mt-1">
                      Will be saved to: <span className="font-mono">{conflict.review_path}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Operations List */}
          <div className="bg-card border border-border rounded-lg p-6">
            <h2 className="text-lg font-semibold mb-4">Operations</h2>
            <div className="space-y-2">
              {manifest.operations.map((op, idx) => {
                const isExpanded = expandedOperations.has(idx.toString());
                return (
                  <div
                    key={idx}
                    className={`border rounded-md overflow-hidden ${getOperationStatusColor(op.status)}`}
                  >
                    <div
                      className="flex items-center gap-3 p-3 cursor-pointer hover:bg-muted/30"
                      onClick={() => toggleOperationExpanded(idx.toString())}
                    >
                      {getOperationStatusIcon(op.status)}
                      <div className="flex-1 min-w-0">
                        <div className="font-mono text-sm truncate">{op.destination}</div>
                        {op.details && (
                          <div className="text-xs opacity-70">{op.details}</div>
                        )}
                      </div>
                      <div className="text-xs font-mono">{op.status}</div>
                      {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </div>
                    {isExpanded && (
                      <div className="px-3 pb-3 pt-1 border-t border-border/50 bg-background/50">
                        <div className="text-xs space-y-1">
                          <div>
                            <span className="font-semibold">Source:</span>{" "}
                            <span className="font-mono">{op.source}</span>
                          </div>
                          <div>
                            <span className="font-semibold">Destination:</span>{" "}
                            <span className="font-mono">{op.destination}</span>
                          </div>
                          {op.details && (
                            <div>
                              <span className="font-semibold">Details:</span> {op.details}
                            </div>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}

      {/* Empty State */}
      {!manifest && !isAnalyzing && !error && (
        <div className="text-center py-16 bg-muted/30 border border-border rounded-lg">
          <FolderSync size={48} className="mx-auto text-muted-foreground mb-4 opacity-50" />
          <h3 className="text-lg font-semibold mb-2">No Migration Analysis Yet</h3>
          <p className="text-muted-foreground mb-6 max-w-md mx-auto">
            Select a session and click "Analyze Migration" to see what files will be migrated.
          </p>
        </div>
      )}
    </div>
  );
}
