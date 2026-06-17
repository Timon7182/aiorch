import { useState, useEffect, useMemo, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Plus,
  Trash2,
  Columns3,
  Terminal,
  FolderOpen,
  BookOpen,
  AlertCircle,
  Download,
  RefreshCw,
  Github,
  GitPullRequest,
  FileText,
  Sparkles,
  GitBranch,
  Wrench,
  Lightbulb,
  MessageCircle,
  Users,
  FileAudio,
  LogOut,
  ChevronDown,
  Check,
  Library,
  Coins,
  ShieldCheck,
  X
} from 'lucide-react';
import { Button } from './ui/button';
import { ScrollArea } from './ui/scroll-area';
import { Separator } from './ui/separator';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from './ui/popover';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger
} from './ui/tooltip';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from './ui/dialog';
import { cn } from '../lib/utils';
import {
  useProjectStore,
  removeProject,
  initializeProject
} from '../stores/project-store';
import { useSettingsStore } from '../stores/settings-store';
import { useAuthStore } from '../stores/auth-store';
import { useAccessStore } from '../stores/access-store';
import { AddProjectModal } from './AddProjectModal';
import { GitSetupModal } from './GitSetupModal';
import { RateLimitIndicator } from './RateLimitIndicator';
import type { Project, AutoBuildVersionInfo, GitStatus, ProjectEnvConfig } from '../shared/types';

export type SidebarView = 'kanban' | 'terminals' | 'editor' | 'context' | 'github-issues' | 'github-prs' | 'changelog' | 'insights' | 'worktrees' | 'agent-tools' | 'skills' | 'hermes' | 'members' | 'transcripts' | 'docs' | 'usage' | 'admin';

interface SidebarProps {
  onSettingsClick: () => void;
  onNewTaskClick: () => void;
  onOpenOnboarding?: () => void;
  activeView?: SidebarView;
  onViewChange?: (view: SidebarView) => void;
  /** When true the sidebar renders as a slide-in overlay drawer (mobile). */
  isMobile?: boolean;
  /** Whether the mobile drawer is currently open. */
  mobileOpen?: boolean;
  /** Called to dismiss the mobile drawer (backdrop tap, nav selection, close button). */
  onMobileClose?: () => void;
}

interface NavItem {
  id: SidebarView;
  labelKey: string;
  icon: React.ElementType;
}

// Base nav items always shown
const baseNavItems: NavItem[] = [
  { id: 'hermes', labelKey: 'Hermes', icon: MessageCircle },
  { id: 'kanban', labelKey: 'navigation:items.kanban', icon: Columns3 },
  { id: 'editor', labelKey: 'navigation:items.editor', icon: FolderOpen },
  { id: 'insights', labelKey: 'navigation:items.chat', icon: Sparkles },
  { id: 'terminals', labelKey: 'navigation:items.terminals', icon: Terminal },
  { id: 'agent-tools', labelKey: 'navigation:items.agentTools', icon: Wrench },
  { id: 'skills', labelKey: 'navigation:items.skills', icon: Lightbulb },
  { id: 'docs', labelKey: 'Docs', icon: Library },
  { id: 'changelog', labelKey: 'navigation:items.changelog', icon: FileText },
  { id: 'usage', labelKey: 'navigation:items.usage', icon: Coins },
  { id: 'worktrees', labelKey: 'navigation:items.worktrees', icon: GitBranch },
  { id: 'context', labelKey: 'navigation:items.context', icon: BookOpen },
  { id: 'transcripts', labelKey: 'Transcripts', icon: FileAudio },
  { id: 'members', labelKey: 'Members', icon: Users }
];

// GitHub nav items shown when GitHub is enabled
const githubNavItems: NavItem[] = [
  { id: 'github-issues', labelKey: 'navigation:items.githubIssues', icon: Github },
  { id: 'github-prs', labelKey: 'navigation:items.githubPRs', icon: GitPullRequest }
];

// Admin-only nav item (global view, not scoped to a project).
const adminNavItem: NavItem = { id: 'admin', labelKey: 'Admin', icon: ShieldCheck };

// Project-scoped views eligible for per-user page filtering. Global views
// (hermes, members, transcripts, admin) are not filtered by project grants.
const PROJECT_SCOPED_VIEWS = new Set<SidebarView>([
  'kanban', 'editor', 'insights', 'terminals', 'agent-tools', 'skills', 'docs',
  'changelog', 'usage', 'worktrees', 'context', 'github-issues', 'github-prs',
]);

export function Sidebar({
  onSettingsClick,
  onNewTaskClick,
  onOpenOnboarding,
  activeView = 'kanban',
  onViewChange,
  isMobile = false,
  mobileOpen = false,
  onMobileClose
}: SidebarProps) {
  const { t } = useTranslation(['navigation', 'dialogs', 'common']);
  const projects = useProjectStore((state) => state.projects);
  const selectedProjectId = useProjectStore((state) => state.selectedProjectId);
  const reposByProject = useProjectStore((state) => state.reposByProject);
  const activeRepoByProject = useProjectStore((state) => state.activeRepoByProject);
  const setActiveRepo = useProjectStore((state) => state.setActiveRepo);
  const settings = useSettingsStore((state) => state.settings);
  const logout = useAuthStore((state) => state.logout);
  const userRole = useAuthStore((state) => state.user?.role);
  // Subscribe to access state so nav items re-filter once grants load.
  const accessUnrestricted = useAccessStore((state) => state.unrestricted);
  const accessProjects = useAccessStore((state) => state.projects);
  const isAdmin = userRole === 'admin';

  const [showAddProjectModal, setShowAddProjectModal] = useState(false);
  const [showInitDialog, setShowInitDialog] = useState(false);
  const [showGitSetupModal, setShowGitSetupModal] = useState(false);
  const [gitStatus, setGitStatus] = useState<GitStatus | null>(null);
  const [pendingProject, setPendingProject] = useState<Project | null>(null);
  const [isInitializing, setIsInitializing] = useState(false);
  const [envConfig, setEnvConfig] = useState<ProjectEnvConfig | null>(null);
  const [showLogoutDialog, setShowLogoutDialog] = useState(false);

  // Persist skipped git setup in localStorage so it survives page refresh
  const [skippedGitSetup, setSkippedGitSetup] = useState<Set<string>>(() => {
    try {
      const saved = localStorage.getItem('skippedGitSetup');
      return saved ? new Set(JSON.parse(saved)) : new Set();
    } catch {
      return new Set();
    }
  });

  // Use ref to access skippedGitSetup in effect without re-running
  const skippedGitSetupRef = useRef(skippedGitSetup);
  skippedGitSetupRef.current = skippedGitSetup;

  // Persist skipped init in localStorage so it survives page refresh
  const [skippedInit, setSkippedInit] = useState<Set<string>>(() => {
    try {
      const saved = localStorage.getItem('skippedInit');
      return saved ? new Set(JSON.parse(saved)) : new Set();
    } catch {
      return new Set();
    }
  });

  // Use ref to access skippedInit in effect without re-running
  const skippedInitRef = useRef(skippedInit);
  skippedInitRef.current = skippedInit;

  const selectedProject = projects.find((p) => p.id === selectedProjectId);

  // Multi-repo switcher: only relevant when a project's root holds >1 git repo.
  const projectRepos = selectedProjectId ? (reposByProject[selectedProjectId] ?? []) : [];
  const isMultiRepo = projectRepos.length > 1;
  const activeRepoPath = selectedProjectId ? activeRepoByProject[selectedProjectId] : undefined;
  const activeRepo = projectRepos.find((r) => r.path === activeRepoPath) ?? projectRepos[0];

  // Load env config when project changes to check GitHub enabled state
  useEffect(() => {
    const loadEnvConfig = async () => {
      if (selectedProject?.autoBuildPath) {
        try {
          const result = await window.API.getProjectEnv(selectedProject.id);
          if (result.success && result.data) {
            setEnvConfig(result.data);
          } else {
            setEnvConfig(null);
          }
        } catch {
          setEnvConfig(null);
        }
      } else {
        setEnvConfig(null);
      }
    };
    loadEnvConfig();
  }, [selectedProject?.id, selectedProject?.autoBuildPath]);

  // Compute visible nav items based on GitHub enabled state, the admin role,
  // and per-user page grants (frontend access filtering).
  const visibleNavItems = useMemo(() => {
    let items = [...baseNavItems];

    if (envConfig?.githubEnabled) {
      items.push(...githubNavItems);
    }

    // Restricted (non-admin) users only see the project-scoped pages granted
    // for the currently selected project. Global views are always shown.
    if (!accessUnrestricted && selectedProjectId) {
      const pages = accessProjects[selectedProjectId];
      items = items.filter((item) => {
        if (!PROJECT_SCOPED_VIEWS.has(item.id)) return true;
        if (pages === undefined) return false; // project not granted
        if (pages === null) return true; // all pages granted
        return pages.includes(item.id);
      });
    }

    if (isAdmin) {
      items.push(adminNavItem);
    }

    return items;
  }, [envConfig?.githubEnabled, isAdmin, accessUnrestricted, accessProjects, selectedProjectId]);

  // Check git status when project changes
  // Use selectedProjectId instead of selectedProject to avoid re-running on every render
  useEffect(() => {
    const checkGit = async () => {
      const project = projects.find((p) => p.id === selectedProjectId);
      if (project) {
        try {
          const result = await window.API.checkGitStatus(project.path);
          if (result.success && result.data) {
            setGitStatus(result.data);
            // Record detected repos so the switcher can offer child repos for
            // multi-repo projects (parent folder holding e.g. backend/ + frontend/).
            if (result.data.repos) {
              useProjectStore.getState().setProjectRepos(project.id, result.data.repos);
            }
            // Show git setup modal if project is not a git repo or has no commits.
            // Skip for multi-repo projects (the repos live in child folders, so
            // initializing git at the parent root would be wrong) and for
            // projects the user has already dismissed.
            if (
              (!result.data.isGitRepo || !result.data.hasCommits) &&
              !result.data.isMultiRepo &&
              !skippedGitSetupRef.current.has(project.id)
            ) {
              setShowGitSetupModal(true);
            }
          }
        } catch (error) {
          console.error('Failed to check git status:', error);
        }
      } else {
        setGitStatus(null);
      }
    };
    checkGit();
    // Only re-run when selectedProjectId changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProjectId]);

  // Check if selected project needs initialization
  useEffect(() => {
    const project = projects.find((p) => p.id === selectedProjectId);
    if (project && !project.autoBuildPath && !skippedInitRef.current.has(project.id)) {
      setPendingProject(project);
      setShowInitDialog(true);
    }
    // Only re-run when selectedProjectId changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProjectId]);

  const handleAddProject = () => {
    setShowAddProjectModal(true);
  };

  const handleProjectAdded = (project: Project, needsInit: boolean) => {
    if (needsInit) {
      setPendingProject(project);
      setShowInitDialog(true);
    }
  };

  const handleInitialize = async () => {
    if (!pendingProject) return;

    const projectId = pendingProject.id;
    setIsInitializing(true);
    try {
      const result = await initializeProject(projectId);
      if (result?.success) {
        // Clear pendingProject FIRST before closing dialog
        // This prevents onOpenChange from triggering skip logic
        setPendingProject(null);
        setShowInitDialog(false);
      }
    } finally {
      setIsInitializing(false);
    }
  };

  const handleSkipInit = () => {
    if (pendingProject) {
      setSkippedInit(prev => {
        const newSet = new Set(prev).add(pendingProject.id);
        localStorage.setItem('skippedInit', JSON.stringify([...newSet]));
        return newSet;
      });
    }
    setShowInitDialog(false);
    setPendingProject(null);
  };

  const handleGitInitialized = async () => {
    // Refresh git status after initialization
    if (selectedProject) {
      try {
        const result = await window.API.checkGitStatus(selectedProject.path);
        if (result.success && result.data) {
          setGitStatus(result.data);
          // Also add to skipped list so modal doesn't show again even if there's a race condition
          setSkippedGitSetup(prev => {
            const newSet = new Set(prev).add(selectedProject.id);
            localStorage.setItem('skippedGitSetup', JSON.stringify([...newSet]));
            return newSet;
          });
        }
      } catch (error) {
        console.error('Failed to refresh git status:', error);
      }
    }
  };

  const _handleRemoveProject = async (projectId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
    await removeProject(projectId);
  };


  const handleNavClick = (view: SidebarView) => {
    onViewChange?.(view);
    // Auto-dismiss the drawer after a selection on mobile so the chosen
    // view is immediately visible instead of hidden behind the overlay.
    if (isMobile) onMobileClose?.();
  };

  const renderNavItem = (item: NavItem) => {
    const isActive = activeView === item.id;
    const Icon = item.icon;
    const alwaysEnabled = item.id === 'hermes' || item.id === 'members' || item.id === 'admin';

    return (
      <button
        key={item.id}
        onClick={() => handleNavClick(item.id)}
        disabled={!alwaysEnabled && !selectedProjectId}
        className={cn(
          'flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-sm transition-all duration-200',
          'hover:bg-primary/10 hover:text-primary',
          'disabled:pointer-events-none disabled:opacity-50',
          isActive && 'bg-primary/20 text-primary font-medium'
        )}
      >
        <Icon className="h-4 w-4 shrink-0" />
        <span className="flex-1 text-left">{t(item.labelKey)}</span>
      </button>
    );
  };

  return (
    <TooltipProvider>
      {/* Mobile backdrop — dims the page behind the drawer and closes it on tap */}
      {isMobile && (
        <div
          className={cn(
            'fixed inset-0 z-40 bg-black/60 backdrop-blur-sm transition-opacity duration-300 md:hidden',
            mobileOpen ? 'opacity-100' : 'pointer-events-none opacity-0'
          )}
          onClick={onMobileClose}
          aria-hidden="true"
        />
      )}
      <div
        className={cn(
          'flex h-full w-64 flex-col border-r border-border bg-sidebar',
          isMobile &&
            'fixed inset-y-0 left-0 z-50 shadow-2xl transition-transform duration-300 ease-in-out will-change-transform',
          isMobile && (mobileOpen ? 'translate-x-0' : '-translate-x-full')
        )}
      >
        {/* Header with drag area - extra top padding for macOS traffic lights */}
        <div className="electron-drag flex h-14 items-center gap-2.5 px-4 pt-6">
          <img src="/logo.png" alt="MagesticAI" className="electron-no-drag h-7 w-7 rounded" />
          <span className="electron-no-drag text-lg font-bold" style={{ color: '#61CE70' }}>Magestic<span style={{ color: '#FFFFFF' }}>AI</span></span>
          {isMobile && (
            <button
              type="button"
              onClick={onMobileClose}
              aria-label="Close menu"
              className="electron-no-drag ml-auto flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground hover:bg-accent/50 hover:text-foreground transition-colors"
            >
              <X className="h-5 w-5" />
            </button>
          )}
        </div>

        <Separator className="mt-2" />

        {/* Navigation */}
        <ScrollArea className="flex-1">
          <div className="px-3 py-4">
            {/* Project Section */}
            <div>
              <h3 className="mb-1 px-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                {t('sections.project')}
              </h3>
              <Popover>
                <PopoverTrigger asChild>
                  <button
                    type="button"
                    className="mb-2 flex w-full items-center justify-between gap-2 rounded-lg px-3 py-2 text-left hover:bg-accent/50 transition-colors"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium">
                        {selectedProject ? selectedProject.name : 'No project selected'}
                      </div>
                      {selectedProject && (
                        <div
                          className="truncate text-[10px] text-muted-foreground/60"
                          title={selectedProject.path}
                        >
                          {selectedProject.path}
                        </div>
                      )}
                    </div>
                    <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
                  </button>
                </PopoverTrigger>
                <PopoverContent className="w-72 p-1" align="start" side="right">
                  <div className="px-2 py-1.5 text-xs font-semibold text-muted-foreground">
                    Projects ({projects.length})
                  </div>
                  <Separator className="my-1" />
                  <div className="max-h-80 overflow-auto">
                    {projects.length === 0 && (
                      <div className="px-2 py-3 text-xs text-muted-foreground">
                        No projects yet. Click <span className="font-medium">Add Project</span> below to start.
                      </div>
                    )}
                    {projects.map((proj) => {
                      const isActive = proj.id === selectedProjectId;
                      return (
                        <button
                          key={proj.id}
                          type="button"
                          onClick={() => {
                            const store = useProjectStore.getState();
                            store.selectProject(proj.id);
                            store.openProjectTab(proj.id);
                          }}
                          className={cn(
                            'flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm hover:bg-accent/50',
                            isActive && 'bg-accent'
                          )}
                        >
                          <div className="flex h-4 w-4 shrink-0 items-center justify-center">
                            {isActive && <Check className="h-3.5 w-3.5" />}
                          </div>
                          <div className="min-w-0 flex-1">
                            <div className="truncate font-medium">{proj.name}</div>
                            <div className="truncate text-[10px] text-muted-foreground/60" title={proj.path}>
                              {proj.path}
                            </div>
                          </div>
                        </button>
                      );
                    })}
                  </div>
                  <Separator className="my-1" />
                  <button
                    type="button"
                    onClick={() => setShowAddProjectModal(true)}
                    className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-sm font-medium text-primary hover:bg-accent/50"
                  >
                    <Plus className="h-4 w-4" />
                    Add or clone a project
                  </button>
                </PopoverContent>
              </Popover>

              {/* Repo switcher — only for projects whose root holds multiple git repos */}
              {isMultiRepo && (
                <Popover>
                  <PopoverTrigger asChild>
                    <button
                      type="button"
                      className="mb-2 flex w-full items-center justify-between gap-2 rounded-lg border border-border/60 bg-accent/30 px-3 py-1.5 text-left hover:bg-accent/50 transition-colors"
                      title={activeRepo?.path}
                    >
                      <div className="flex min-w-0 flex-1 items-center gap-2">
                        <GitBranch className="h-3.5 w-3.5 shrink-0 text-primary" />
                        <span className="truncate text-xs font-medium">
                          {activeRepo?.name ?? 'Select repo'}
                        </span>
                      </div>
                      <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                    </button>
                  </PopoverTrigger>
                  <PopoverContent className="w-72 p-1" align="start" side="right">
                    <div className="px-2 py-1.5 text-xs font-semibold text-muted-foreground">
                      Repositories ({projectRepos.length})
                    </div>
                    <Separator className="my-1" />
                    {projectRepos.map((repo) => {
                      const isActive = repo.path === (activeRepo?.path);
                      return (
                        <button
                          key={repo.path}
                          type="button"
                          onClick={() => selectedProjectId && setActiveRepo(selectedProjectId, repo.path)}
                          className={cn(
                            'flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm hover:bg-accent/50',
                            isActive && 'bg-accent'
                          )}
                        >
                          <div className="flex h-4 w-4 shrink-0 items-center justify-center">
                            {isActive && <Check className="h-3.5 w-3.5" />}
                          </div>
                          <div className="min-w-0 flex-1">
                            <div className="truncate font-medium">{repo.name}</div>
                            <div className="truncate text-[10px] text-muted-foreground/60" title={repo.path}>
                              {repo.path}
                            </div>
                          </div>
                        </button>
                      );
                    })}
                  </PopoverContent>
                </Popover>
              )}

              <nav className="space-y-1">
                {visibleNavItems.map(renderNavItem)}
              </nav>
            </div>
          </div>
        </ScrollArea>

        <Separator />

        {/* Rate Limit Indicator - shows when Claude is rate limited */}
        <RateLimitIndicator />

        {/* Bottom section with New Task */}
        <div className="p-4 space-y-3">
          {/* New Task button */}
          <Button
            className="w-full bg-primary hover:bg-primary/90 text-primary-foreground"
            onClick={onNewTaskClick}
            disabled={!selectedProjectId || !selectedProject?.autoBuildPath}
          >
            <Plus className="mr-2 h-4 w-4" />
            {t('actions.newTask')}
          </Button>
          {selectedProject && !selectedProject.autoBuildPath && (
            <Button
              variant="outline"
              size="sm"
              className="w-full mt-2"
              onClick={() => {
                setPendingProject(selectedProject);
                setShowInitDialog(true);
              }}
            >
              <Download className="mr-2 h-3.5 w-3.5" />
              {t('messages.initializeToCreateTasks')}
            </Button>
          )}

          {/* Logout */}
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                onClick={() => setShowLogoutDialog(true)}
                className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-xs text-muted-foreground hover:bg-destructive/10 hover:text-destructive transition-colors"
              >
                <LogOut className="h-3.5 w-3.5" />
                {t('actions.logout')}
              </button>
            </TooltipTrigger>
            <TooltipContent side="right">
              {t('tooltips.logout')}
            </TooltipContent>
          </Tooltip>
        </div>
      </div>

      {/* Initialize Magestic AI Dialog */}
      <Dialog open={showInitDialog} onOpenChange={(open) => {
        // Only allow closing if user manually closes (not during initialization)
        if (!open && !isInitializing) {
          handleSkipInit();
        }
      }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Download className="h-5 w-5" />
              {t('dialogs:initialize.title')}
            </DialogTitle>
            <DialogDescription>
              {t('dialogs:initialize.description')}
            </DialogDescription>
          </DialogHeader>
          <div className="py-4">
            <div className="rounded-lg bg-muted p-4 text-sm">
              <p className="font-medium mb-2">{t('dialogs:initialize.willDo')}</p>
              <ul className="list-disc list-inside space-y-1 text-muted-foreground">
                <li>{t('dialogs:initialize.createFolder')}</li>
                <li>{t('dialogs:initialize.copyFramework')}</li>
                <li>{t('dialogs:initialize.setupSpecs')}</li>
              </ul>
            </div>
            {!settings.autoBuildPath && (
              <div className="mt-4 rounded-lg border border-warning/50 bg-warning/10 p-4 text-sm">
                <div className="flex items-start gap-2">
                  <AlertCircle className="h-4 w-4 text-warning mt-0.5 shrink-0" />
                  <div>
                    <p className="font-medium text-warning">{t('dialogs:initialize.sourcePathNotConfigured')}</p>
                    <p className="text-muted-foreground mt-1">
                      {t('dialogs:initialize.sourcePathNotConfiguredDescription')}
                    </p>
                  </div>
                </div>
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={handleSkipInit} disabled={isInitializing}>
              {t('common:buttons.skip')}
            </Button>
            <Button
              onClick={handleInitialize}
              disabled={isInitializing || !settings.autoBuildPath}
            >
              {isInitializing ? (
                <>
                  <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
                  {t('common:labels.initializing')}
                </>
              ) : (
                <>
                  <Download className="mr-2 h-4 w-4" />
                  {t('common:buttons.initialize')}
                </>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Add Project Modal */}
      <AddProjectModal
        open={showAddProjectModal}
        onOpenChange={setShowAddProjectModal}
        onProjectAdded={handleProjectAdded}
      />

      {/* Git Setup Modal */}
      <GitSetupModal
        open={showGitSetupModal}
        onOpenChange={setShowGitSetupModal}
        project={selectedProject || null}
        gitStatus={gitStatus}
        onGitInitialized={handleGitInitialized}
        onSkip={() => {
          if (selectedProject) {
            setSkippedGitSetup(prev => {
              const newSet = new Set(prev).add(selectedProject.id);
              localStorage.setItem('skippedGitSetup', JSON.stringify([...newSet]));
              return newSet;
            });
          }
        }}
      />

      {/* Logout Confirmation Dialog */}
      <Dialog open={showLogoutDialog} onOpenChange={setShowLogoutDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <LogOut className="h-5 w-5" />
              {t('logoutDialog.title')}
            </DialogTitle>
            <DialogDescription>
              {t('logoutDialog.description')}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowLogoutDialog(false)}>
              {t('logoutDialog.cancel')}
            </Button>
            <Button variant="destructive" onClick={() => { setShowLogoutDialog(false); logout(); }}>
              <LogOut className="mr-2 h-4 w-4" />
              {t('logoutDialog.confirm')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </TooltipProvider>
  );
}
