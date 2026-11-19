// frontend/src/features/workspace/types.ts

// ==============================================================================
// 1. Secure Workspace File Operations (Optimistic Concurrency Control)
// ==============================================================================

/**
 * Response schema for reading a file from the workspace.
 * Matches backend: WorkspaceFileReadResponse
 */
export interface WorkspaceFileReadResponse {
  path: string;
  content: string;
  version: string; // SHA256 hash of the current file content
  mime_type: string;
}

/**
 * Request schema for updating a file in the workspace with OCC.
 * Matches backend: WorkspaceFileUpdateRequest
 */
export interface WorkspaceFileUpdateRequest {
  path: string;
  content: string;
  expected_version: string; // Expected SHA256 hash for Optimistic Concurrency Control
}

/**
 * Response schema returned when an OCC conflict (409) occurs.
 * Matches backend: WorkspaceFileUpdateConflictResponse
 */
export interface WorkspaceFileUpdateConflictResponse {
  error: string;
  message: string;
  current_version: string; // Current SHA256 hash of the file on the server
}

/**
 * Custom error class for handling OCC conflicts in the frontend.
 */
export class OCCConflictError extends Error {
  public readonly currentVersion: string;
  public readonly path: string;

  constructor(path: string, currentVersion: string, message: string) {
    super(message);
    this.name = "OCCConflictError";
    this.path = path;
    this.currentVersion = currentVersion;
  }
}

// ==============================================================================
// 2. Git Repository Discovery
// ==============================================================================

/**
 * Schema representing a discovered Git repository within the project workspace.
 * Matches backend: GitRepositoryInfo
 */
export interface GitRepositoryInfo {
  id: string;
  name: string;
  root_path: string;
  git_status: "clean" | "dirty" | "unknown";
  current_branch: string | null;
}

// ==============================================================================
// 3. Artifact Metadata
// ==============================================================================

/**
 * Schema for artifact metadata, serving as the source of truth.
 * Matches backend: ArtifactMetadataResponse
 */
export interface ArtifactMetadataResponse {
  artifact_id: string;
  project_id: string;
  session_id: string | null;
  job_id: string;
  storage_scope: "project_workspace" | "session_storage";
  artifact_type: string;
  relative_path: string;
  created_at: string; // ISO 8601 timestamp
}

// ==============================================================================
// 4. Migration Engine Types
// ==============================================================================

/**
 * Represents a single operation in the migration manifest.
 */
export interface MigrationOperation {
  source: string;
  destination: string;
  status: "pending" | "success" | "skipped" | "conflict" | "conflict_resolved" | "failed" | "rolled_back" | "rollback_failed";
  details?: string;
}

/**
 * Represents a conflict detected during migration.
 */
export interface MigrationConflict {
  source: string;
  destination: string;
  review_path: string;
  source_hash: string;
  dest_hash: string;
}

/**
 * Full migration manifest returned by the backend.
 */
export interface MigrationManifest {
  project_id: string;
  session_id: string;
  timestamp: string;
  operations: MigrationOperation[];
  conflicts: MigrationConflict[];
}

// ==============================================================================
// 5. Task Management Types
// ==============================================================================

/**
 * Valid task statuses.
 */
export type TaskStatus = "todo" | "in_progress" | "review" | "done" | "blocked";

/**
 * Valid task priorities.
 */
export type TaskPriority = "low" | "medium" | "high" | "critical";

/**
 * Valid relationship types between tasks.
 */
export type TaskRelationshipType = "depends_on" | "blocks" | "parent_of" | "related_to";

/**
 * Represents a relationship between two tasks.
 */
export interface TaskRelationship {
  source_task_id: string;
  target_task_id: string;
  relationship_type: TaskRelationshipType;
}

/**
 * Represents a task in the project.
 */
export interface Task {
  id: string;
  project_id: string;
  title: string;
  description?: string;
  status: TaskStatus;
  priority: TaskPriority;
  assignee?: string;
  created_at: string;
  updated_at: string;
  metadata: Record<string, any>;
  relationships: TaskRelationship[];
}

/**
 * Represents an entry in the audit log.
 */
export interface AuditLogEntry {
  id: number;
  task_id: string;
  actor: string;
  timestamp: string;
  action: string;
  previous_state?: string;
  new_state?: string;
  reason?: string;
}

// ==============================================================================
// 6. Project Types
// ==============================================================================

/**
 * Represents the workspace type for a project.
 */
export type WorkspaceType = "internal" | "external";

/**
 * Represents a project with its workspace configuration.
 */
export interface Project {
  id: string;
  name: string;
  description?: string;
  workspace_path: string;
  workspace_type: WorkspaceType;
  created_at: string;
  updated_at: string;
}

// ==============================================================================
// 7. Event Types (SSE)
// ==============================================================================

/**
 * Event types emitted by the backend via Server-Sent Events.
 */
export type WorkspaceEventType =
  | "artifact_created"
  | "file_modified"
  | "file_deleted"
  | "task_status_changed"
  | "git_status_changed";

/**
 * Base interface for all workspace events.
 */
export interface WorkspaceEvent {
  type: WorkspaceEventType;
  project_id: string;
  timestamp: string;
  payload: any;
}

/**
 * Event payload for file modifications.
 */
export interface FileModifiedEvent extends WorkspaceEvent {
  type: "file_modified";
  payload: {
    path: string;
    version: string;
    modified_by: string;
  };
}

/**
 * Event payload for artifact creation.
 */
export interface ArtifactCreatedEvent extends WorkspaceEvent {
  type: "artifact_created";
  payload: ArtifactMetadataResponse;
}
