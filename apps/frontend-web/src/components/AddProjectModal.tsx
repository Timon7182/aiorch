import { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { FolderOpen, Search, Loader2, GitBranch, Package, FileCode, CheckCircle, FileText, FolderPlus, Upload, X } from 'lucide-react';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { Label } from './ui/label';
import { ScrollArea } from './ui/scroll-area';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from './ui/dialog';
import { cn } from '../lib/utils';
import { addProject } from '../stores/project-store';
import { getAuthHeaders } from '../lib/auth';
import type { Project } from '../shared/types';

const DOC_SUFFIXES = ['.md', '.markdown', '.txt', '.rst', '.org'];

type IngestResult = {
  saved: number;
  rejected: string[];
  indexed_sections: number;
  files_indexed: number;
};

async function uploadDocs(projectSlug: string, files: File[]): Promise<IngestResult> {
  const form = new FormData();
  for (const f of files) form.append('files', f);
  const res = await fetch(`/api/ext/projects/${encodeURIComponent(projectSlug)}/ingest-docs`, {
    method: 'POST',
    headers: getAuthHeaders(),
    body: form,
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const j = (await res.json()) as { detail?: unknown };
      if (j?.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return (await res.json()) as IngestResult;
}

interface DiscoveredProject {
  name: string;
  path: string;
  has_git: boolean;
  has_package_json: boolean;
  has_requirements: boolean;
  has_magestic_ai: boolean;
  has_claude_md: boolean;
}

interface AddProjectModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onProjectAdded?: (project: Project, needsInit: boolean) => void;
}

// Default projects folder - can be customized
const DEFAULT_PROJECTS_FOLDER = '/home';

export function AddProjectModal({ open, onOpenChange, onProjectAdded }: AddProjectModalProps) {
  const { t } = useTranslation('dialogs');
  const [projectsFolder, setProjectsFolder] = useState(DEFAULT_PROJECTS_FOLDER);
  const [discoveredProjects, setDiscoveredProjects] = useState<DiscoveredProject[]>([]);
  const [selectedProject, setSelectedProject] = useState<DiscoveredProject | null>(null);
  const [customPath, setCustomPath] = useState('');
  const [isScanning, setIsScanning] = useState(false);
  const [isAdding, setIsAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [useCustomPath, setUseCustomPath] = useState(false);
  const [showClaudeReadyOnly, setShowClaudeReadyOnly] = useState(false);
  const [createdDirPath, setCreatedDirPath] = useState<string | null>(null);

  // step 2: optional doc upload
  const [addedProject, setAddedProject] = useState<Project | null>(null);
  const [docFiles, setDocFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<IngestResult | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const projectSlug = addedProject ? (addedProject.name || '').toLowerCase().replace(/[^a-z0-9-_]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '') || addedProject.id : '';

  // Filter and sort projects - Claude-ready projects first
  const sortedProjects = [...discoveredProjects].sort((a, b) => {
    // Projects with CLAUDE.md come first
    if (a.has_claude_md && !b.has_claude_md) return -1;
    if (!a.has_claude_md && b.has_claude_md) return 1;
    // Then by name
    return a.name.localeCompare(b.name);
  });

  const filteredProjects = showClaudeReadyOnly
    ? sortedProjects.filter(p => p.has_claude_md)
    : sortedProjects;

  const claudeReadyCount = discoveredProjects.filter(p => p.has_claude_md).length;

  // Scan for projects when folder changes
  const scanProjects = useCallback(async () => {
    if (!projectsFolder.trim()) return;

    setIsScanning(true);
    setError(null);
    setDiscoveredProjects([]);
    setSelectedProject(null);

    try {
      const result = await window.API.discoverProjects(projectsFolder, 2);
      if (result.success && result.data) {
        setDiscoveredProjects(result.data);
        if (result.data.length === 0) {
          setError('No projects found in this folder. Try a different path or enter a custom path below.');
        }
      } else {
        setError(result.error || 'Failed to scan for projects');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to scan for projects');
    } finally {
      setIsScanning(false);
    }
  }, [projectsFolder]);

  // Scan on modal open
  useEffect(() => {
    if (open) {
      setCustomPath('');
      setSelectedProject(null);
      setUseCustomPath(false);
      setShowClaudeReadyOnly(false);
      setError(null);
      setCreatedDirPath(null);
      setAddedProject(null);
      setDocFiles([]);
      setUploadResult(null);
      scanProjects();
    }
  }, [open, scanProjects]);

  const handleAddProject = async () => {
    const path = useCustomPath ? customPath.trim() : selectedProject?.path;
    if (!path) {
      setError('Please select a project or enter a custom path');
      return;
    }

    setIsAdding(true);
    setError(null);

    try {
      const project = await addProject(path);
      if (project) {
        // Try to detect and save main branch
        try {
          const mainBranchResult = await window.API.detectMainBranch(path);
          if (mainBranchResult.success && mainBranchResult.data) {
            await window.API.updateProjectSettings(project.id, {
              mainBranch: mainBranchResult.data
            });
          }
        } catch {
          // Non-fatal - main branch can be set later
        }
        onProjectAdded?.(project, !project.autoBuildPath);
        if (project.createdDirectory) {
          setCreatedDirPath(project.path);
        }
        // Switch to step 2: optional docs upload (modal stays open)
        setAddedProject(project);
      } else {
        setError('Failed to add project. Please check the path is valid.');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add project');
    } finally {
      setIsAdding(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !isAdding && !isScanning) {
      if (e.target instanceof HTMLInputElement && e.target.id === 'projects-folder') {
        scanProjects();
      } else {
        handleAddProject();
      }
    }
  };

  const onPickFiles = (files: FileList | File[] | null) => {
    if (!files) return;
    const list = Array.from(files).filter((f) =>
      DOC_SUFFIXES.some((s) => f.name.toLowerCase().endsWith(s)),
    );
    setDocFiles((prev) => {
      const seen = new Set(prev.map((f) => f.name));
      return [...prev, ...list.filter((f) => !seen.has(f.name))];
    });
  };

  const handleUploadDocs = async () => {
    if (!addedProject || docFiles.length === 0) return;
    setUploading(true);
    setError(null);
    try {
      const result = await uploadDocs(projectSlug, docFiles);
      setUploadResult(result);
    } catch (e) {
      setError(`Upload failed: ${(e as Error).message}`);
    } finally {
      setUploading(false);
    }
  };

  if (addedProject) {
    return (
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>
              <div className="flex items-center gap-2">
                <CheckCircle className="h-5 w-5 text-green-500" />
                Project added — index documentation?
              </div>
            </DialogTitle>
            <DialogDescription>
              Drop markdown notes, meeting transcripts, or any plain-text docs.
              They&apos;ll be indexed so Hermes can cite them when you chat.
            </DialogDescription>
          </DialogHeader>

          <div className="py-4 space-y-4">
            <div className="rounded-lg bg-muted p-3 text-xs">
              <div><strong>{addedProject.name}</strong></div>
              <div className="text-muted-foreground">{addedProject.path}</div>
            </div>

            <div
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                onPickFiles(e.dataTransfer.files);
              }}
              onClick={() => fileInputRef.current?.click()}
              className="border-2 border-dashed border-border rounded-lg p-6 text-center cursor-pointer hover:bg-accent/30 transition-colors"
            >
              <Upload className="h-7 w-7 mx-auto mb-2 text-muted-foreground" />
              <p className="text-sm font-medium">Drop files here, or click to browse</p>
              <p className="text-xs text-muted-foreground mt-1">
                Accepts: {DOC_SUFFIXES.join(', ')} — max 5 MB each
              </p>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept={DOC_SUFFIXES.join(',')}
                onChange={(e) => onPickFiles(e.target.files)}
                className="hidden"
              />
            </div>

            {docFiles.length > 0 && (
              <ScrollArea className="max-h-[160px] rounded-md border">
                <div className="p-2 space-y-1">
                  {docFiles.map((f) => (
                    <div key={f.name} className="flex items-center justify-between text-xs px-2 py-1 rounded hover:bg-accent/30">
                      <span className="truncate">{f.name}</span>
                      <span className="text-muted-foreground tabular-nums ml-2">
                        {(f.size / 1024).toFixed(1)} KB
                      </span>
                      <button
                        type="button"
                        onClick={() => setDocFiles((prev) => prev.filter((x) => x !== f))}
                        className="ml-2 p-1 rounded hover:bg-destructive/10 text-destructive"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </div>
                  ))}
                </div>
              </ScrollArea>
            )}

            {uploadResult && (
              <div className="text-sm text-green-700 dark:text-green-400 bg-green-500/10 rounded-lg p-3">
                Indexed <strong>{uploadResult.files_indexed}</strong> file(s) into{' '}
                <strong>{uploadResult.indexed_sections}</strong> searchable section(s).
                {uploadResult.rejected.length > 0 && (
                  <div className="mt-1 text-xs text-yellow-700 dark:text-yellow-400">
                    Rejected: {uploadResult.rejected.join(', ')}
                  </div>
                )}
              </div>
            )}

            {error && (
              <div className="text-sm text-destructive bg-destructive/10 rounded-lg p-3">
                {error}
              </div>
            )}
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              {uploadResult ? 'Done' : 'Skip'}
            </Button>
            <Button onClick={handleUploadDocs} disabled={uploading || docFiles.length === 0}>
              {uploading ? (
                <><Loader2 className="mr-2 h-4 w-4 animate-spin" />Indexing…</>
              ) : (
                <><Upload className="mr-2 h-4 w-4" />Index {docFiles.length || ''} file{docFiles.length === 1 ? '' : 's'}</>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            <div className="flex items-center gap-2">
              <FolderOpen className="h-5 w-5" />
              {t('addProject.title', 'Add Project')}
            </div>
          </DialogTitle>
          <DialogDescription>
            Select a project from your projects folder or enter a custom path.
          </DialogDescription>
        </DialogHeader>

        <div className="py-4 space-y-4">
          {/* Projects folder input */}
          <div className="space-y-2">
            <Label htmlFor="projects-folder">Projects Folder</Label>
            <div className="flex gap-2">
              <Input
                id="projects-folder"
                placeholder="/home/user/projects"
                value={projectsFolder}
                onChange={(e) => setProjectsFolder(e.target.value)}
                onKeyDown={handleKeyDown}
              />
              <Button
                variant="outline"
                onClick={scanProjects}
                disabled={isScanning || !projectsFolder.trim()}
              >
                {isScanning ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
              </Button>
            </div>
          </div>

          {/* Discovered projects list */}
          {!useCustomPath && discoveredProjects.length > 0 && (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label>
                  Available Projects ({filteredProjects.length})
                  {claudeReadyCount > 0 && (
                    <span className="ml-2 text-xs text-blue-500">
                      ({claudeReadyCount} Claude-ready)
                    </span>
                  )}
                </Label>
                {claudeReadyCount > 0 && (
                  <Button
                    variant={showClaudeReadyOnly ? "default" : "outline"}
                    size="sm"
                    onClick={() => setShowClaudeReadyOnly(!showClaudeReadyOnly)}
                    className="h-6 text-xs"
                  >
                    <FileText className="h-3 w-3 mr-1" />
                    {showClaudeReadyOnly ? "Show All" : "CLAUDE.md Only"}
                  </Button>
                )}
              </div>
              <ScrollArea className="h-[200px] rounded-md border">
                <div className="p-2 space-y-1">
                  {filteredProjects.map((proj) => (
                    <button
                      key={proj.path}
                      onClick={() => setSelectedProject(proj)}
                      className={cn(
                        'w-full flex items-center gap-3 rounded-lg px-3 py-2 text-left transition-colors',
                        'hover:bg-accent/50',
                        selectedProject?.path === proj.path && 'bg-accent border border-primary'
                      )}
                    >
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="font-medium text-sm truncate">{proj.name}</span>
                          {proj.has_magestic_ai && (
                            <CheckCircle className="h-3 w-3 text-green-500 shrink-0" />
                          )}
                        </div>
                        <p className="text-xs text-muted-foreground truncate">{proj.path}</p>
                      </div>
                      <div className="flex items-center gap-1 shrink-0">
                        {proj.has_claude_md && <span title="Has CLAUDE.md"><FileText className="h-3 w-3 text-blue-500" /></span>}
                        {proj.has_git && <span title="Git repository"><GitBranch className="h-3 w-3 text-muted-foreground" /></span>}
                        {proj.has_package_json && <span title="Node.js project"><Package className="h-3 w-3 text-muted-foreground" /></span>}
                        {proj.has_requirements && <span title="Python project"><FileCode className="h-3 w-3 text-muted-foreground" /></span>}
                      </div>
                    </button>
                  ))}
                </div>
              </ScrollArea>
            </div>
          )}

          {/* Loading state */}
          {isScanning && (
            <div className="flex items-center justify-center py-8 text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin mr-2" />
              Scanning for projects...
            </div>
          )}

          {/* Toggle for custom path */}
          <div className="flex items-center gap-2">
            <input
              type="checkbox"
              id="use-custom"
              checked={useCustomPath}
              onChange={(e) => {
                setUseCustomPath(e.target.checked);
                if (e.target.checked) setSelectedProject(null);
              }}
              className="h-4 w-4 rounded"
            />
            <Label htmlFor="use-custom" className="text-sm font-normal cursor-pointer">
              Enter custom path instead
            </Label>
          </div>

          {/* Custom path input */}
          {useCustomPath && (
            <div className="space-y-2">
              <Label htmlFor="custom-path">Custom Project Path</Label>
              <Input
                id="custom-path"
                placeholder="/home/user/projects/my-project"
                value={customPath}
                onChange={(e) => setCustomPath(e.target.value)}
                onKeyDown={handleKeyDown}
                autoFocus
              />
            </div>
          )}

          {/* Directory created info */}
          {createdDirPath && (
            <div className="text-sm text-green-700 dark:text-green-400 bg-green-500/10 rounded-lg p-3 flex items-center gap-2">
              <FolderPlus className="h-4 w-4 shrink-0" />
              <span>Created new directory: <strong>{createdDirPath}</strong></span>
            </div>
          )}

          {/* Error message */}
          {error && !isScanning && (
            <div className="text-sm text-destructive bg-destructive/10 rounded-lg p-3">
              {error}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={isAdding}>
            Cancel
          </Button>
          <Button
            onClick={handleAddProject}
            disabled={isAdding || isScanning || (!selectedProject && !useCustomPath) || (useCustomPath && !customPath.trim())}
          >
            {isAdding ? 'Adding...' : 'Add Project'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
