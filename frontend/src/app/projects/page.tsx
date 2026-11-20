// frontend/src/app/projects/page.tsx

/**
 * Projects List Page
 *
 * Main entry point for the projects section.
 * Displays all projects with filtering, search, and creation capabilities.
 *
 * Features:
 * - Grid/List view toggle
 * - Search and filter projects
 * - Quick actions (create, delete, open)
 * - Project statistics overview
 * - Integration with useProjectStore for state management
 */

"use client";

import React, { useState, useMemo } from "react";
import { useRouter } from "next/navigation";
import {
  Plus,
  Search,
  Grid3x3,
  List,
  Folder,
  Calendar,
  MessageSquare,
  MoreVertical,
  Trash2,
  Settings,
  Loader2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { useProjectStore } from "@/stores/project-store";

type ViewMode = "grid" | "list";

export default function ProjectsPage() {
  const router = useRouter();
  const { getOrderedProjects, isLoading, loadProjects, removeProject } = useProjectStore();

  const [viewMode, setViewMode] = useState<ViewMode>("grid");
  const [searchQuery, setSearchQuery] = useState("");
  const [sortBy, setSortBy] = useState<"name" | "created_at">("created_at");
  const [chatCounts, setChatCounts] = useState<Record<string, number>>({});
  const [isInitialLoad, setIsInitialLoad] = useState(true);

  // Load projects on mount
  React.useEffect(() => {
    const fetchProjects = async () => {
      try {
        // On F5, isInitialized is false, so loadProjects() will fetch automatically.
        // We do NOT pass `true` here to avoid the store's "forced load honors empty result"
        // heuristic wiping the state if there's a transient network glitch.
        await loadProjects();
      } catch (error) {
        console.error("[ProjectsPage] Failed to load projects:", error);
      } finally {
        setIsInitialLoad(false);
      }
    };

    fetchProjects();
  }, [loadProjects]);

  // Get all projects (excluding default_project)
  const allProjects = useMemo(() => {
    return getOrderedProjects().filter((p) => p.id !== "default_project");
  }, [getOrderedProjects]);

  // Filter and sort projects
  const filteredProjects = useMemo(() => {
    let filtered = allProjects;

    // Search filter
    if (searchQuery.trim()) {
      const query = searchQuery.toLowerCase();
      filtered = filtered.filter(
        (p) =>
          p.name.toLowerCase().includes(query) ||
          p.description?.toLowerCase().includes(query)
      );
    }

    // Sort
    filtered = [...filtered].sort((a, b) => {
      if (sortBy === "name") {
        return a.name.localeCompare(b.name);
      }
      return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
    });

    return filtered;
  }, [allProjects, searchQuery, sortBy]);

  // Fetch chat counts for filtered projects in parallel
  React.useEffect(() => {
    let cancelled = false;

    const fetchChatCounts = async () => {
      const counts: Record<string, number> = {};

      await Promise.all(
        filteredProjects.map(async (project) => {
          try {
            const res = await fetch(
              `/api/chats/?project_id=${encodeURIComponent(project.id)}&limit=1`,
              { credentials: "include", headers: { Accept: "application/json" } }
            );
            if (res.ok) {
              const data = await res.json();
              counts[project.id] = typeof data?.total === "number" ? data.total : 0;
            } else {
              counts[project.id] = 0;
            }
          } catch {
            counts[project.id] = 0;
          }
        })
      );

      if (!cancelled) {
        setChatCounts(counts);
      }
    };

    if (filteredProjects.length > 0) {
      fetchChatCounts();
    }

    return () => {
      cancelled = true;
    };
  }, [filteredProjects]);

  const handleCreateProject = () => {
    router.push("/projects/new");
  };

  const handleOpenProject = (projectId: string) => {
    router.push(`/projects/${projectId}`);
  };

  const handleDeleteProject = async (projectId: string, projectName: string) => {
    if (!confirm(`Are you sure you want to delete "${projectName}"? This action cannot be undone.`)) {
      return;
    }

    try {
      // Deletion lives on chats.router at /api/chats/projects/{id}.
      // projects.py has no DELETE handler for the root project path,
      // so calling /api/projects/{id} would return 405.
      const res = await fetch(`/api/chats/projects/${projectId}`, {
        method: "DELETE",
      });
      if (res.status === 409) {
        const body = await res.json().catch(() => ({}));
        alert(
          body.detail ||
            "Cannot delete: a run is still active. Stop it first."
        );
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      removeProject(projectId);
    } catch (error: any) {
      console.error("Failed to delete project:", error);
      alert(error?.message || "Failed to delete project. Please try again.");
    }
  };

  const formatDate = (dateString: string) => {
    const date = new Date(dateString);
    return date.toLocaleDateString("en-US", {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  };

  // Show loader while explicitly waiting for the initial fetch to complete
  if (isInitialLoad) {
    return (
      <div className="container mx-auto px-4 py-8">
        <div className="flex items-center justify-center h-64">
          <Loader2 className="animate-spin text-primary" size={32} />
        </div>
      </div>
    );
  }

  return (
    <div className="container mx-auto px-4 py-8 max-w-7xl">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-3xl font-bold">Projects</h1>
          <p className="text-muted-foreground mt-2">
            Manage your projects and collaborate with AI agents
          </p>
        </div>
        <Button onClick={handleCreateProject} className="gap-2">
          <Plus size={16} />
          Create Project
        </Button>
      </div>

      {/* Toolbar */}
      <div className="flex items-center gap-4 mb-6">
        {/* Search */}
        <div className="flex-1 relative">
          <Search
            size={16}
            className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground"
          />
          <input
            type="text"
            placeholder="Search projects..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full h-10 pl-10 pr-4 bg-background border border-border rounded-md
                       focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary"
          />
        </div>

        {/* Sort */}
        <select
          value={sortBy}
          onChange={(e) => setSortBy(e.target.value as "name" | "created_at")}
          className="h-10 px-3 bg-background border border-border rounded-md
                     focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary
                     cursor-pointer"
        >
          <option value="created_at">Sort by Date</option>
          <option value="name">Sort by Name</option>
        </select>

        {/* View Toggle */}
        <div className="flex items-center gap-1 bg-muted rounded-md p-1">
          <button
            onClick={() => setViewMode("grid")}
            className={`p-2 rounded ${
              viewMode === "grid"
                ? "bg-background shadow-sm"
                : "hover:bg-background/50"
            }`}
            title="Grid view"
          >
            <Grid3x3 size={16} />
          </button>
          <button
            onClick={() => setViewMode("list")}
            className={`p-2 rounded ${
              viewMode === "list"
                ? "bg-background shadow-sm"
                : "hover:bg-background/50"
            }`}
            title="List view"
          >
            <List size={16} />
          </button>
        </div>
      </div>

      {/* Projects Grid/List */}
      {filteredProjects.length === 0 ? (
        <div className="text-center py-16">
          <Folder size={48} className="mx-auto text-muted-foreground mb-4" />
          <h3 className="text-lg font-semibold mb-2">
            {searchQuery ? "No projects found" : "No projects yet"}
          </h3>
          <p className="text-muted-foreground mb-6">
            {searchQuery
              ? "Try adjusting your search query"
              : "Create your first project to get started"}
          </p>
          {!searchQuery && (
            <Button onClick={handleCreateProject} className="gap-2">
              <Plus size={16} />
              Create Project
            </Button>
          )}
        </div>
      ) : viewMode === "grid" ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {filteredProjects.map((project) => (
            <div
              key={project.id}
              className="group relative bg-card border border-border rounded-lg p-6
                         hover:shadow-lg transition-all cursor-pointer"
              onClick={() => handleOpenProject(project.id)}
            >
              {/* Project Header */}
              <div className="flex items-start justify-between mb-4">
                <div className="flex-1 min-w-0">
                  <h3 className="text-lg font-semibold truncate mb-1">
                    {project.name}
                  </h3>
                  <p className="text-sm text-muted-foreground line-clamp-2">
                    {project.description || "No description"}
                  </p>
                </div>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    // TODO: Implement dropdown menu
                  }}
                  className="p-1 rounded hover:bg-muted opacity-0 group-hover:opacity-100 transition-opacity"
                >
                  <MoreVertical size={16} />
                </button>
              </div>

              {/* Project Stats */}
              <div className="flex items-center gap-4 text-xs text-muted-foreground mb-4">
                <div className="flex items-center gap-1">
                  <Calendar size={12} />
                  <span>{formatDate(project.created_at)}</span>
                </div>
                <div className="flex items-center gap-1">
                  <MessageSquare size={12} />
                  <span>{chatCounts[project.id] ?? 0} chats</span>
                </div>
              </div>

              {/* Project Actions */}
              <div className="flex items-center gap-2 pt-4 border-t border-border">
                <Button
                  variant="outline"
                  size="sm"
                  className="flex-1 gap-1.5"
                  onClick={(e) => {
                    e.stopPropagation();
                    handleOpenProject(project.id);
                  }}
                >
                  <Folder size={14} />
                  Open
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="gap-1.5"
                  onClick={(e) => {
                    e.stopPropagation();
                    router.push(`/projects/${project.id}/settings`);
                  }}
                >
                  <Settings size={14} />
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="gap-1.5 text-destructive hover:text-destructive"
                  onClick={(e) => {
                    e.stopPropagation();
                    handleDeleteProject(project.id, project.name);
                  }}
                >
                  <Trash2 size={14} />
                </Button>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="space-y-2">
          {filteredProjects.map((project) => (
            <div
              key={project.id}
              className="group flex items-center gap-4 bg-card border border-border rounded-lg p-4
                         hover:shadow-md transition-all cursor-pointer"
              onClick={() => handleOpenProject(project.id)}
            >
              <Folder size={24} className="text-primary shrink-0" />
              <div className="flex-1 min-w-0">
                <h3 className="font-semibold truncate">{project.name}</h3>
                <p className="text-sm text-muted-foreground truncate">
                  {project.description || "No description"}
                </p>
              </div>
              <div className="flex items-center gap-4 text-sm text-muted-foreground">
                <div className="flex items-center gap-1">
                  <Calendar size={14} />
                  <span>{formatDate(project.created_at)}</span>
                </div>
                <div className="flex items-center gap-1">
                  <MessageSquare size={14} />
                  <span>{chatCounts[project.id] ?? 0} chats</span>
                </div>
              </div>
              <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={(e) => {
                    e.stopPropagation();
                    router.push(`/projects/${project.id}/settings`);
                  }}
                >
                  <Settings size={16} />
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-destructive hover:text-destructive"
                  onClick={(e) => {
                    e.stopPropagation();
                    handleDeleteProject(project.id, project.name);
                  }}
                >
                  <Trash2 size={16} />
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Projects Count */}
      {filteredProjects.length > 0 && (
        <div className="mt-6 text-center text-sm text-muted-foreground">
          Showing {filteredProjects.length} of {allProjects.length} projects
        </div>
      )}
    </div>
  );
}
