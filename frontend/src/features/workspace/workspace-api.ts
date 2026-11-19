// frontend/src/features/workspace/workspace-api.ts

import {
  WorkspaceFileReadResponse,
  WorkspaceFileUpdateRequest,
  WorkspaceFileUpdateConflictResponse,
  OCCConflictError,
  GitRepositoryInfo,
  ArtifactMetadataResponse,
  MigrationManifest,
  Task,
  AuditLogEntry,
} from "./types";

/**
 * Base API client for workspace operations.
 * Handles authentication, error handling, and response parsing.
 */
class WorkspaceApiClient {
  private baseUrl: string;

  constructor(baseUrl: string = "/api") {
    this.baseUrl = baseUrl;
  }

  /**
   * Generic request handler with error parsing.
   */
  private async request<T>(
    endpoint: string,
    options: RequestInit = {}
  ): Promise<T> {
    const url = `${this.baseUrl}${endpoint}`;

    const response = await fetch(url, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...options.headers,
      },
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));

      // Handle OCC conflict specifically
      if (response.status === 409) {
        const conflictData = errorData as WorkspaceFileUpdateConflictResponse;
        throw new OCCConflictError(
          conflictData.message || "Conflict occurred",
          conflictData.current_version,
          conflictData.message
        );
      }

      throw new Error(
        errorData.detail || errorData.message || `HTTP ${response.status}: ${response.statusText}`
      );
    }

    return response.json();
  }

  // ============================================================================
  // Workspace File Operations
  // ============================================================================

  /**
   * Reads a file from the project workspace.
   */
  async readFile(projectId: string, path: string): Promise<WorkspaceFileReadResponse> {
    const encodedPath = encodeURIComponent(path);
    return this.request<WorkspaceFileReadResponse>(
      `/projects/${projectId}/workspace/file?path=${encodedPath}`
    );
  }

  /**
   * Updates a file in the project workspace with Optimistic Concurrency Control.
   * @throws {OCCConflictError} If the file has been modified by another process
   */
  async updateFile(
    projectId: string,
    data: WorkspaceFileUpdateRequest
  ): Promise<WorkspaceFileReadResponse> {
    return this.request<WorkspaceFileReadResponse>(
      `/projects/${projectId}/workspace/file`,
      {
        method: "PUT",
        body: JSON.stringify(data),
      }
    );
  }

  /**
   * Lists all files in the project workspace.
   */
  async listFiles(projectId: string, path: string = ""): Promise<string[]> {
    const encodedPath = encodeURIComponent(path);
    return this.request<string[]>(
      `/projects/${projectId}/workspace/files?path=${encodedPath}`
    );
  }

  /**
   * Deletes a file from the project workspace.
   */
  async deleteFile(projectId: string, path: string): Promise<void> {
    const encodedPath = encodeURIComponent(path);
    await this.request<void>(
      `/projects/${projectId}/workspace/file?path=${encodedPath}`,
      {
        method: "DELETE",
      }
    );
  }

  // ============================================================================
  // Git Repository Discovery
  // ============================================================================

  /**
   * Discovers all Git repositories within the project workspace.
   */
  async discoverRepositories(projectId: string): Promise<GitRepositoryInfo[]> {
    return this.request<GitRepositoryInfo[]>(
      `/projects/${projectId}/source-control/repositories`
    );
  }

  /**
   * Gets the current status of a specific repository.
   */
  async getRepositoryStatus(
    projectId: string,
    repositoryId: string
  ): Promise<GitRepositoryInfo> {
    return this.request<GitRepositoryInfo>(
      `/projects/${projectId}/source-control/repositories/${repositoryId}/status`
    );
  }

  // ============================================================================
  // Artifact Metadata
  // ============================================================================

  /**
   * Retrieves artifact metadata for a project.
   */
  async getArtifacts(
    projectId: string,
    sessionId?: string
  ): Promise<ArtifactMetadataResponse[]> {
    const params = sessionId ? `?session_id=${sessionId}` : "";
    return this.request<ArtifactMetadataResponse[]>(
      `/projects/${projectId}/artifacts${params}`
    );
  }

  /**
   * Retrieves a specific artifact by ID.
   */
  async getArtifact(
    projectId: string,
    artifactId: string
  ): Promise<ArtifactMetadataResponse> {
    return this.request<ArtifactMetadataResponse>(
      `/projects/${projectId}/artifacts/${artifactId}`
    );
  }

  // ============================================================================
  // Migration Engine
  // ============================================================================

  /**
   * Performs a dry-run migration analysis.
   */
  async dryRunMigration(
    projectId: string,
    sessionId: string
  ): Promise<MigrationManifest> {
    return this.request<MigrationManifest>(
      `/projects/${projectId}/migration/dry-run`,
      {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId }),
      }
    );
  }

  /**
   * Executes the migration based on a manifest.
   */
  async executeMigration(
    projectId: string,
    sessionId: string,
    manifest?: MigrationManifest
  ): Promise<MigrationManifest> {
    return this.request<MigrationManifest>(
      `/projects/${projectId}/migration/execute`,
      {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, manifest }),
      }
    );
  }

  /**
   * Rolls back a previous migration.
   */
  async rollbackMigration(
    projectId: string,
    manifest: MigrationManifest
  ): Promise<MigrationManifest> {
    return this.request<MigrationManifest>(
      `/projects/${projectId}/migration/rollback`,
      {
        method: "POST",
        body: JSON.stringify(manifest),
      }
    );
  }

  // ============================================================================
  // Task Management
  // ============================================================================

  /**
   * Retrieves all tasks for a project.
   */
  async getProjectTasks(
    projectId: string,
    statusFilter?: string
  ): Promise<Task[]> {
    const params = statusFilter ? `?status=${statusFilter}` : "";
    return this.request<Task[]>(
      `/projects/${projectId}/tasks${params}`
    );
  }

  /**
   * Retrieves a specific task by ID.
   */
  async getTask(projectId: string, taskId: string): Promise<Task> {
    return this.request<Task>(
      `/projects/${projectId}/tasks/${taskId}`
    );
  }

  /**
   * Creates a new task.
   */
  async createTask(
    projectId: string,
    task: Omit<Task, "id" | "project_id" | "created_at" | "updated_at" | "relationships">
  ): Promise<Task> {
    return this.request<Task>(
      `/projects/${projectId}/tasks`,
      {
        method: "POST",
        body: JSON.stringify(task),
      }
    );
  }

  /**
   * Updates task status with validation and audit logging.
   */
  async updateTaskStatus(
    projectId: string,
    taskId: string,
    newStatus: string,
    actor: string,
    reason?: string
  ): Promise<Task> {
    return this.request<Task>(
      `/projects/${projectId}/tasks/${taskId}/status`,
      {
        method: "PATCH",
        body: JSON.stringify({ status: newStatus, actor, reason }),
      }
    );
  }

  /**
   * Retrieves audit log for a task or entire project.
   */
  async getAuditLog(
    projectId: string,
    taskId?: string,
    limit: number = 100
  ): Promise<AuditLogEntry[]> {
    const params = new URLSearchParams();
    if (taskId) params.append("task_id", taskId);
    params.append("limit", limit.toString());

    return this.request<AuditLogEntry[]>(
      `/projects/${projectId}/audit-log?${params.toString()}`
    );
  }
}

/**
 * Singleton instance of the Workspace API client.
 */
export const workspaceApi = new WorkspaceApiClient();
