// frontend/src/app/projects/[projectId]/tasks/page.tsx

/**
 * Project Tasks Page - TaskBoard
 *
 * Displays and manages tasks for a specific project.
 * Features:
 * - Task list with filtering by status
 * - Create new task
 * - Update task status with validation
 * - View audit log for tasks
 * - Kanban-style columns or list view
 *
 * Design:
 * - Uses workspaceApi for all task operations
 * - Integrates with the new Task management system
 * - Provides visual status indicators
 * - Shows audit trail for transparency
 */

"use client";

import React, { useState, useEffect, useCallback } from "react";
import { useParams } from "next/navigation";
import {
  CheckSquare,
  Plus,
  Loader2,
  AlertCircle,
  Filter,
  History,
  X,
  Edit,
  Trash2,
  Clock,
  User,
  Flag,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { workspaceApi } from "@/features/workspace/workspace-api";
import { Task, TaskStatus, TaskPriority, AuditLogEntry } from "@/features/workspace/types";

type ViewMode = "list" | "kanban";

export default function TasksPage() {
  const params = useParams();
  const projectId = params.projectId as string;

  const [tasks, setTasks] = useState<Task[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>("list");
  const [statusFilter, setStatusFilter] = useState<TaskStatus | "all">("all");
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [showAuditLog, setShowAuditLog] = useState(false);
  const [auditLog, setAuditLog] = useState<AuditLogEntry[]>([]);

  // Load tasks on mount
  useEffect(() => {
    loadTasks();
  }, [projectId, statusFilter]);

  const loadTasks = async () => {
    setIsLoading(true);
    setError(null);

    try {
      const filter = statusFilter === "all" ? undefined : statusFilter;
      const loadedTasks = await workspaceApi.getProjectTasks(projectId, filter);
      setTasks(loadedTasks);
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Failed to load tasks";
      setError(errorMessage);
      console.error("[TasksPage] Failed to load tasks:", err);
    } finally {
      setIsLoading(false);
    }
  };

  const handleCreateTask = async (taskData: {
    title: string;
    description?: string;
    priority: TaskPriority;
    assignee?: string;
  }) => {
    try {
      // Add required fields: status and metadata
      const newTask = await workspaceApi.createTask(projectId, {
        ...taskData,
        status: "todo" as TaskStatus,
        metadata: {},
      });
      setTasks((prev) => [newTask, ...prev]);
      setShowCreateModal(false);
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Failed to create task";
      alert(errorMessage);
    }
  };

  const handleUpdateStatus = async (taskId: string, newStatus: TaskStatus) => {
    try {
      const updatedTask = await workspaceApi.updateTaskStatus(
        projectId,
        taskId,
        newStatus,
        "user",
        `Status changed to ${newStatus}`
      );
      setTasks((prev) => prev.map((t) => (t.id === taskId ? updatedTask : t)));
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Failed to update task status";
      alert(errorMessage);
    }
  };

  const handleViewAuditLog = async (taskId: string) => {
    try {
      const log = await workspaceApi.getAuditLog(projectId, taskId);
      setAuditLog(log);
      setShowAuditLog(true);
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Failed to load audit log";
      alert(errorMessage);
    }
  };

  const getStatusColor = (status: TaskStatus) => {
    switch (status) {
      case "todo":
        return "bg-slate-500";
      case "in_progress":
        return "bg-blue-500";
      case "review":
        return "bg-purple-500";
      case "done":
        return "bg-emerald-500";
      case "blocked":
        return "bg-rose-500";
      default:
        return "bg-slate-400";
    }
  };

  const getPriorityIcon = (priority: TaskPriority) => {
    switch (priority) {
      case "critical":
        return <Flag size={12} className="text-rose-500" />;
      case "high":
        return <Flag size={12} className="text-orange-500" />;
      case "medium":
        return <Flag size={12} className="text-yellow-500" />;
      case "low":
        return <Flag size={12} className="text-slate-400" />;
      default:
        return null;
    }
  };

  if (isLoading && tasks.length === 0) {
    return (
      <div className="container mx-auto px-4 py-8">
        <div className="flex items-center justify-center h-64">
          <Loader2 size={32} className="animate-spin text-primary" />
        </div>
      </div>
    );
  }

  return (
    <div className="container mx-auto px-4 py-8 max-w-7xl">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <CheckSquare className="text-primary" size={24} />
            Project Tasks
          </h1>
          <p className="text-muted-foreground mt-1">
            Manage and track tasks within this project.
          </p>
        </div>
        <Button onClick={() => setShowCreateModal(true)} className="gap-2">
          <Plus size={16} />
          New Task
        </Button>
      </div>

      {/* Toolbar */}
      <div className="flex items-center gap-4 mb-6">
        {/* Status Filter */}
        <div className="flex items-center gap-2">
          <Filter size={16} className="text-muted-foreground" />
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as TaskStatus | "all")}
            className="h-9 px-3 bg-background border border-border rounded-md text-sm
                       focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary"
          >
            <option value="all">All Statuses</option>
            <option value="todo">To Do</option>
            <option value="in_progress">In Progress</option>
            <option value="review">In Review</option>
            <option value="done">Done</option>
            <option value="blocked">Blocked</option>
          </select>
        </div>

        {/* View Mode Toggle */}
        <div className="flex items-center gap-1 bg-muted rounded-md p-1">
          <button
            onClick={() => setViewMode("list")}
            className={`px-3 py-1.5 rounded text-sm ${
              viewMode === "list"
                ? "bg-background shadow-sm"
                : "hover:bg-background/50"
            }`}
          >
            List
          </button>
          <button
            onClick={() => setViewMode("kanban")}
            className={`px-3 py-1.5 rounded text-sm ${
              viewMode === "kanban"
                ? "bg-background shadow-sm"
                : "hover:bg-background/50"
            }`}
          >
            Kanban
          </button>
        </div>

        {/* Task Count */}
        <div className="ml-auto text-sm text-muted-foreground">
          {tasks.length} task{tasks.length !== 1 ? "s" : ""}
        </div>
      </div>

      {/* Error State */}
      {error && (
        <div className="mb-6 p-4 bg-destructive/10 border border-destructive/30 rounded-lg flex items-start gap-3">
          <AlertCircle className="text-destructive shrink-0 mt-0.5" size={18} />
          <div>
            <h3 className="font-semibold text-destructive mb-1">Failed to load tasks</h3>
            <p className="text-sm text-muted-foreground">{error}</p>
          </div>
        </div>
      )}

      {/* Empty State */}
      {!isLoading && tasks.length === 0 && !error && (
        <div className="text-center py-16 bg-muted/30 border border-border rounded-lg">
          <CheckSquare size={48} className="mx-auto text-muted-foreground mb-4 opacity-50" />
          <h3 className="text-lg font-semibold mb-2">No tasks yet</h3>
          <p className="text-muted-foreground mb-6 max-w-md mx-auto">
            Create your first task to start tracking work within this project.
          </p>
          <Button onClick={() => setShowCreateModal(true)} className="gap-2">
            <Plus size={16} />
            Create First Task
          </Button>
        </div>
      )}

      {/* Task List View */}
      {viewMode === "list" && tasks.length > 0 && (
        <div className="space-y-3">
          {tasks.map((task) => (
            <div
              key={task.id}
              className="group bg-card border border-border rounded-lg p-4 hover:shadow-md transition-all"
            >
              <div className="flex items-start gap-4">
                {/* Status Indicator */}
                <div
                  className={`w-3 h-3 rounded-full shrink-0 mt-1.5 ${getStatusColor(task.status)}`}
                  title={`Status: ${task.status}`}
                />

                {/* Task Content */}
                <div className="flex-1 min-w-0">
                  <div className="flex items-start justify-between gap-2 mb-2">
                    <h3 className="font-semibold text-base">{task.title}</h3>
                    <div className="flex items-center gap-1 shrink-0">
                      {getPriorityIcon(task.priority)}
                      <span className="text-xs text-muted-foreground capitalize">
                        {task.priority}
                      </span>
                    </div>
                  </div>

                  {task.description && (
                    <p className="text-sm text-muted-foreground mb-3 line-clamp-2">
                      {task.description}
                    </p>
                  )}

                  <div className="flex items-center gap-4 text-xs text-muted-foreground">
                    {task.assignee && (
                      <div className="flex items-center gap-1">
                        <User size={12} />
                        <span>{task.assignee}</span>
                      </div>
                    )}
                    <div className="flex items-center gap-1">
                      <Clock size={12} />
                      <span>Created {new Date(task.created_at).toLocaleDateString()}</span>
                    </div>
                  </div>
                </div>

                {/* Actions */}
                <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setSelectedTask(task)}
                    className="h-8 w-8 p-0"
                    title="View details"
                  >
                    <Edit size={14} />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleViewAuditLog(task.id)}
                    className="h-8 w-8 p-0"
                    title="View audit log"
                  >
                    <History size={14} />
                  </Button>
                </div>
              </div>

              {/* Status Update Buttons */}
              <div className="flex items-center gap-2 mt-3 pt-3 border-t border-border">
                <span className="text-xs text-muted-foreground">Status:</span>
                {(["todo", "in_progress", "review", "done", "blocked"] as TaskStatus[]).map(
                  (status) => (
                    <button
                      key={status}
                      onClick={() => handleUpdateStatus(task.id, status)}
                      disabled={task.status === status}
                      className={`px-2 py-1 text-xs rounded border transition-colors ${
                        task.status === status
                          ? "bg-primary/10 border-primary text-primary"
                          : "bg-background border-border hover:bg-muted"
                      } disabled:cursor-not-allowed`}
                    >
                      {status.replace("_", " ")}
                    </button>
                  )
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Kanban View */}
      {viewMode === "kanban" && tasks.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-3 lg:grid-cols-5 gap-4">
          {(["todo", "in_progress", "review", "done", "blocked"] as TaskStatus[]).map(
            (status) => {
              const statusTasks = tasks.filter((t) => t.status === status);
              return (
                <div key={status} className="bg-muted/30 border border-border rounded-lg p-3">
                  <div className="flex items-center gap-2 mb-3 pb-2 border-b border-border">
                    <div className={`w-2 h-2 rounded-full ${getStatusColor(status)}`} />
                    <h3 className="text-sm font-semibold capitalize">
                      {status.replace("_", " ")}
                    </h3>
                    <span className="ml-auto text-xs text-muted-foreground">
                      {statusTasks.length}
                    </span>
                  </div>
                  <div className="space-y-2">
                    {statusTasks.map((task) => (
                      <div
                        key={task.id}
                        className="bg-card border border-border rounded-md p-3 hover:shadow-sm transition-all cursor-pointer"
                        onClick={() => setSelectedTask(task)}
                      >
                        <h4 className="text-sm font-medium mb-1">{task.title}</h4>
                        {task.description && (
                          <p className="text-xs text-muted-foreground line-clamp-2 mb-2">
                            {task.description}
                          </p>
                        )}
                        <div className="flex items-center gap-2 text-xs text-muted-foreground">
                          {getPriorityIcon(task.priority)}
                          {task.assignee && (
                            <div className="flex items-center gap-1">
                              <User size={10} />
                              <span>{task.assignee}</span>
                            </div>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              );
            }
          )}
        </div>
      )}

      {/* Create Task Modal */}
      {showCreateModal && (
        <CreateTaskModal
          onClose={() => setShowCreateModal(false)}
          onCreate={handleCreateTask}
        />
      )}

      {/* Task Details Modal */}
      {selectedTask && (
        <TaskDetailsModal
          task={selectedTask}
          onClose={() => setSelectedTask(null)}
          onUpdate={loadTasks}
        />
      )}

      {/* Audit Log Modal */}
      {showAuditLog && (
        <AuditLogModal
          auditLog={auditLog}
          onClose={() => setShowAuditLog(false)}
        />
      )}
    </div>
  );
}

// ============================================================================
// Create Task Modal
// ============================================================================

interface CreateTaskModalProps {
  onClose: () => void;
  onCreate: (taskData: {
    title: string;
    description?: string;
    priority: TaskPriority;
    assignee?: string;
  }) => void;
}

function CreateTaskModal({ onClose, onCreate }: CreateTaskModalProps) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState<TaskPriority>("medium");
  const [assignee, setAssignee] = useState("");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;

    onCreate({
      title: title.trim(),
      description: description.trim() || undefined,
      priority,
      assignee: assignee.trim() || undefined,
    });
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-card border border-border rounded-lg p-6 max-w-md w-full">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-semibold">Create New Task</h2>
          <Button variant="ghost" size="sm" onClick={onClose}>
            <X size={16} />
          </Button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium mb-1">Title *</label>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="w-full px-3 py-2 bg-background border border-border rounded-md
                         focus:outline-none focus:ring-2 focus:ring-primary/50"
              autoFocus
              required
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">Description</label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="w-full px-3 py-2 bg-background border border-border rounded-md
                         focus:outline-none focus:ring-2 focus:ring-primary/50"
              rows={3}
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">Priority</label>
            <select
              value={priority}
              onChange={(e) => setPriority(e.target.value as TaskPriority)}
              className="w-full px-3 py-2 bg-background border border-border rounded-md
                         focus:outline-none focus:ring-2 focus:ring-primary/50"
            >
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
              <option value="critical">Critical</option>
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">Assignee</label>
            <input
              type="text"
              value={assignee}
              onChange={(e) => setAssignee(e.target.value)}
              className="w-full px-3 py-2 bg-background border border-border rounded-md
                         focus:outline-none focus:ring-2 focus:ring-primary/50"
              placeholder="Optional"
            />
          </div>

          <div className="flex gap-2 pt-4">
            <Button type="submit" className="flex-1">
              Create Task
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
// Task Details Modal
// ============================================================================

interface TaskDetailsModalProps {
  task: Task;
  onClose: () => void;
  onUpdate: () => void;
}

function TaskDetailsModal({ task, onClose, onUpdate }: TaskDetailsModalProps) {
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-card border border-border rounded-lg p-6 max-w-2xl w-full max-h-[80vh] overflow-auto">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-semibold">{task.title}</h2>
          <Button variant="ghost" size="sm" onClick={onClose}>
            <X size={16} />
          </Button>
        </div>

        <div className="space-y-4">
          <div>
            <label className="text-sm font-medium text-muted-foreground">Description</label>
            <p className="mt-1">{task.description || "No description"}</p>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="text-sm font-medium text-muted-foreground">Status</label>
              <p className="mt-1 capitalize">{task.status.replace("_", " ")}</p>
            </div>
            <div>
              <label className="text-sm font-medium text-muted-foreground">Priority</label>
              <p className="mt-1 capitalize">{task.priority}</p>
            </div>
            <div>
              <label className="text-sm font-medium text-muted-foreground">Assignee</label>
              <p className="mt-1">{task.assignee || "Unassigned"}</p>
            </div>
            <div>
              <label className="text-sm font-medium text-muted-foreground">Created</label>
              <p className="mt-1">{new Date(task.created_at).toLocaleString()}</p>
            </div>
          </div>

          {task.relationships.length > 0 && (
            <div>
              <label className="text-sm font-medium text-muted-foreground">Relationships</label>
              <div className="mt-1 space-y-1">
                {task.relationships.map((rel, idx) => (
                  <div key={idx} className="text-sm">
                    {rel.relationship_type}: {rel.target_task_id}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="flex gap-2 pt-4 border-t border-border mt-4">
          <Button variant="outline" onClick={onClose} className="flex-1">
            Close
          </Button>
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// Audit Log Modal
// ============================================================================

interface AuditLogModalProps {
  auditLog: AuditLogEntry[];
  onClose: () => void;
}

function AuditLogModal({ auditLog, onClose }: AuditLogModalProps) {
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-card border border-border rounded-lg p-6 max-w-3xl w-full max-h-[80vh] overflow-auto">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-semibold flex items-center gap-2">
            <History size={20} />
            Audit Log
          </h2>
          <Button variant="ghost" size="sm" onClick={onClose}>
            <X size={16} />
          </Button>
        </div>

        {auditLog.length === 0 ? (
          <div className="text-center py-8 text-muted-foreground">
            No audit log entries found.
          </div>
        ) : (
          <div className="space-y-2">
            {auditLog.map((entry) => (
              <div
                key={entry.id}
                className="bg-muted/30 border border-border rounded-md p-3"
              >
                <div className="flex items-start justify-between gap-2 mb-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">{entry.action}</span>
                    <span className="text-xs text-muted-foreground">by {entry.actor}</span>
                  </div>
                  <span className="text-xs text-muted-foreground">
                    {new Date(entry.timestamp).toLocaleString()}
                  </span>
                </div>
                {entry.previous_state && entry.new_state && (
                  <div className="text-xs text-muted-foreground">
                    {entry.previous_state} → {entry.new_state}
                  </div>
                )}
                {entry.reason && (
                  <div className="text-xs text-muted-foreground mt-1">
                    Reason: {entry.reason}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        <div className="flex gap-2 pt-4 border-t border-border mt-4">
          <Button variant="outline" onClick={onClose} className="flex-1">
            Close
          </Button>
        </div>
      </div>
    </div>
  );
}
