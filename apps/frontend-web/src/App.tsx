import { useState, useEffect, useMemo, useCallback } from 'react';
import { Routes, Route, Navigate, useNavigate, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { TooltipProvider } from './components/ui/tooltip';
import { Toaster } from './components/ui/toaster';
import { Sidebar, type SidebarView } from './components/Sidebar';
import { ProjectTabBar } from './components/ProjectTabBar';
import { MobileTopBar } from './components/MobileTopBar';
import { KanbanBoard } from './components/KanbanBoard';
import { TerminalGrid } from './components/TerminalGrid';
import { Worktrees } from './components/Worktrees';
import { Context } from './components/context/Context';
import { GitHubIssues } from './components/GitHubIssues';
import { GitHubPRs } from './components/github-prs/GitHubPRs';
import { Changelog } from './components/changelog/Changelog';
import { Insights } from './components/Insights';
import { UsageView } from './components/UsageView';
import { AgentTools } from './components/AgentTools';
import { SkillsPage } from './components/SkillsPage';
import { WelcomeScreen } from './components/WelcomeScreen';
import { AddProjectModal } from './components/AddProjectModal';
import { DocumentationView } from './components/DocumentationView';
import { AppSettingsDialog } from './components/settings';
import { TaskCreationWizard } from './components/TaskCreationWizard';
import { TaskDetailPage } from './components/task-detail';
import { OnboardingWizard } from './components/onboarding';
import { LoadingScreen } from './components/LoadingScreen';
import { ProjectSwitchLoadingModal } from './components/ProjectSwitchLoadingModal';
import { LoginPage } from './pages/LoginPage';
import { EditorPage } from './pages/EditorPage';
import { HermesPage } from './pages/HermesPage';
import { PendingApprovalScreen } from './pages/PendingApprovalScreen';
import { MembersPage } from './pages/MembersPage';
import { TranscriptsPage } from './pages/TranscriptsPage';
import { AdminPage } from './pages/AdminPage';
import { ViewStateProvider } from './contexts/ViewStateContext';
import { useProjectStore, loadProjects } from './stores/project-store';
import { useTaskStore, loadTasks } from './stores/task-store';
import { useSettingsStore, loadSettings } from './stores/settings-store';
import { useAuthStore } from './stores/auth-store';
import { useIpcListeners } from './hooks/useIpc';
import { useIsMobile } from './hooks/use-media-query';
import { useWorkspaceRoute, GLOBAL_VIEWS, DEFAULT_PROJECT_VIEW } from './hooks/use-workspace-route';
import { cn } from './lib/utils';
import { UI_SCALE_MIN, UI_SCALE_MAX, UI_SCALE_DEFAULT } from './shared/constants';
import type { Task, Project } from './shared/types';

function AuthenticatedApp() {
  // Loading screen state - show for 5 seconds on every page load
  const [isLoading, setIsLoading] = useState(true);

  const handleLoadingComplete = useCallback(() => {
    setIsLoading(false);
  }, []);

  // Stores
  const projects = useProjectStore((state) => state.projects);
  const selectedProjectId = useProjectStore((state) => state.selectedProjectId);
  const activeProjectId = useProjectStore((state) => state.activeProjectId);
  const openProjectIds = useProjectStore((state) => state.openProjectIds);
  const tabOrder = useProjectStore((state) => state.tabOrder);
  const openProjectTab = useProjectStore((state) => state.openProjectTab);
  const closeProjectTab = useProjectStore((state) => state.closeProjectTab);
  const setActiveProject = useProjectStore((state) => state.setActiveProject);
  const isSwitchingProject = useProjectStore((state) => state.isSwitchingProject);
  const tasks = useTaskStore((state) => state.tasks);
  const settings = useSettingsStore((state) => state.settings);

  // Set up IPC event listeners for real-time task updates via WebSocket
  useIpcListeners();

  // Compute open projects for the tab bar (respecting tab order)
  const openProjects = useMemo(() => {
    // Get projects in tab order first
    const orderedProjects = tabOrder
      .map((id) => projects.find((p) => p.id === id))
      .filter((p): p is Project => p !== undefined && openProjectIds.includes(p.id));

    // Add any open projects not in tabOrder to the end
    const remainingProjects = projects.filter(
      (p) => openProjectIds.includes(p.id) && !tabOrder.includes(p.id)
    );

    return [...orderedProjects, ...remainingProjects];
  }, [projects, openProjectIds, tabOrder]);

  // URL is the source of truth for which project/view/task is active, so every
  // screen is linkable. We derive these from the path instead of useState.
  const navigate = useNavigate();
  //const location = useLocation();
  const route = useWorkspaceRoute();
  const routeProjectId = route.projectId;
  const activeView: SidebarView = route.view ?? DEFAULT_PROJECT_VIEW;
  const selectedTaskId = route.taskId;

  // Derive selectedTask from store so it updates when store changes
  // (status changes, subtask updates, execution progress, etc.)
  const selectedTask = useMemo(
    () => {
      if (!selectedTaskId) return null;
      const task = tasks.find(t => t.id === selectedTaskId || t.specId === selectedTaskId) ?? null;
      if (window.DEBUG && task) {
        console.log('[App] selectedTask derived:', task.id, 'status:', task.status, 'subtasks:', task.subtasks?.length);
      }
      return task;
    },
    [selectedTaskId, tasks]
  );
  const isMobile = useIsMobile();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [isNewTaskDialogOpen, setIsNewTaskDialogOpen] = useState(false);
  const [isSettingsDialogOpen, setIsSettingsDialogOpen] = useState(false);
  const [isAddProjectModalOpen, setIsAddProjectModalOpen] = useState(false);
  const [isOnboardingOpen, setIsOnboardingOpen] = useState(false);
  // Seed for the next Task Creation Wizard open (e.g. after creating a project
  // from a natural-language prompt). Cleared once consumed so it doesn't leak
  // into subsequent opens.
  const [taskWizardSeed, setTaskWizardSeed] = useState<{
    title?: string;
    description?: string;
  } | null>(null);

  // selectedProject follows the URL; for global views (no project in the path)
  // it falls back to the store's active project so the tab bar/wizard still work.
  const selectedProject = projects.find((p) => p.id === routeProjectId);
  const currentProjectId = routeProjectId ?? activeProjectId ?? selectedProjectId ?? null;
  const dashboardProject = selectedProject ?? projects.find((p) => p.id === currentProjectId) ?? null;

  // Navigate to a sidebar view (global views live at /:view, project views at /p/:id/:view).
  const handleViewChange = useCallback((v: SidebarView) => {
    if ((GLOBAL_VIEWS as readonly string[]).includes(v)) {
      navigate(`/${v}`);
      return;
    }
    if (currentProjectId) {
      navigate(`/p/${currentProjectId}/${v}`);
    } else {
      navigate('/');
    }
  }, [currentProjectId, navigate]);

  // Open a task as a full page with its own URL.
  const handleTaskClick = useCallback((task: Task) => {
    if (currentProjectId) {
      navigate(`/p/${currentProjectId}/tasks/${task.id}`);
    }
  }, [currentProjectId, navigate]);

  // Close the task page, returning to the project board.
  const handleCloseTask = useCallback(() => {
    navigate(currentProjectId ? `/p/${currentProjectId}/kanban` : '/');
  }, [currentProjectId, navigate]);

  // Compute project name for loading modal
  const switchingProjectName = useMemo(() => {
    if (!isSwitchingProject || !activeProjectId) return undefined;
    return projects.find(p => p.id === activeProjectId)?.name;
  }, [isSwitchingProject, activeProjectId, projects]);

  // Initial load
  useEffect(() => {
    loadProjects();
    loadSettings();
  }, []);

  // Trigger onboarding only if CLI not installed or auth token missing
  useEffect(() => {
    if (settings.onboardingCompleted !== false) return;

    // Check actual setup status before showing wizard
    const checkSetup = async () => {
      try {
        const [versionResult, authResult] = await Promise.all([
          window.API?.checkClaudeCodeVersion?.() ?? { success: false },
          window.API?.getAuthStatus?.() ?? { success: false },
        ]);

        const cliInstalled = versionResult.success && versionResult.data?.installed;
        const hasToken = authResult.success && authResult.data?.hasToken;

        if (cliInstalled && hasToken) {
          // Everything is set up — skip wizard and mark as completed
          const { updateSettings } = useSettingsStore.getState();
          updateSettings({ onboardingCompleted: true });
          window.API?.saveSettings?.({ onboardingCompleted: true });
        } else {
          setIsOnboardingOpen(true);
        }
      } catch {
        // If checks fail, show wizard as fallback
        setIsOnboardingOpen(true);
      }
    };

    checkSetup();
  }, [settings.onboardingCompleted]);

  // Sync i18n language with settings
  const { i18n } = useTranslation();
  useEffect(() => {
    if (settings.language && settings.language !== i18n.language) {
      i18n.changeLanguage(settings.language);
    }
  }, [settings.language, i18n]);

  // Load tasks when project changes
  useEffect(() => {
    const pid = activeProjectId || selectedProjectId;
    if (pid) {
      loadTasks(pid);
    } else {
      useTaskStore.getState().clearTasks();
    }
  }, [activeProjectId, selectedProjectId]);

  // Sync the project store to the project in the URL so child components
  // (which read selectedProjectId from the store) follow deep links. An
  // unknown project id bounces back to root.
  useEffect(() => {
    if (!routeProjectId) return;
    if (projects.length === 0) return; // wait for projects to load
    const proj = projects.find((p) => p.id === routeProjectId);
    if (!proj) {
      navigate('/', { replace: true });
      return;
    }
    if (activeProjectId !== routeProjectId) {
      setActiveProject(routeProjectId);
      useProjectStore.getState().selectProject(routeProjectId);
      openProjectTab(routeProjectId);
    }
  }, [routeProjectId, projects, activeProjectId, navigate, setActiveProject, openProjectTab]);

  // From the bare root, jump into the last active project's board. Only
  // redirect to a project that still exists, otherwise a stale id would
  // bounce back to root and loop.
  /*useEffect(() => {
    if (location.pathname !== '/') return;
    const pid = activeProjectId || selectedProjectId;
    if (pid && projects.some((p) => p.id === pid)) {
      navigate(`/p/${pid}/kanban`, { replace: true });
    }
  }, [location.pathname, activeProjectId, selectedProjectId, projects, navigate]);*/


  // Safety timeout: auto-clear stuck switching state after 10 seconds
  useEffect(() => {
    if (!isSwitchingProject) return;
    const timeout = setTimeout(() => {
      useProjectStore.getState().clearSwitchingState();
    }, 10_000);
    return () => clearTimeout(timeout);
  }, [isSwitchingProject]);

  // Apply theme (light/dark mode + Ocean color theme)
  useEffect(() => {
    const root = document.documentElement;

    // Always use Ocean color theme
    root.setAttribute('data-theme', 'ocean');

    const applyTheme = () => {
      if (settings.theme === 'dark') {
        root.classList.add('dark');
      } else if (settings.theme === 'light') {
        root.classList.remove('dark');
      } else {
        if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
          root.classList.add('dark');
        } else {
          root.classList.remove('dark');
        }
      }
    };

    applyTheme();

    // Persist to localStorage so the inline script in index.html can apply
    // the theme synchronously on next load, preventing a flash of wrong colors
    try {
      localStorage.setItem('magestic-theme', settings.theme ?? 'system');
    } catch {
      // localStorage may be unavailable
    }

    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
    const handleChange = () => {
      if (settings.theme === 'system') {
        applyTheme();
      }
    };
    mediaQuery.addEventListener('change', handleChange);

    return () => {
      mediaQuery.removeEventListener('change', handleChange);
    };
  }, [settings.theme]);

  // Apply UI scale
  useEffect(() => {
    const root = document.documentElement;
    const scale = settings.uiScale ?? UI_SCALE_DEFAULT;
    const clampedScale = Math.max(UI_SCALE_MIN, Math.min(UI_SCALE_MAX, scale));
    root.setAttribute('data-ui-scale', clampedScale.toString());
  }, [settings.uiScale]);

  // Close the mobile drawer when the viewport grows to desktop so a stale
  // "open" state can't leave the overlay lingering after a resize/rotate.
  useEffect(() => {
    if (!isMobile) setSidebarOpen(false);
  }, [isMobile]);

  const handleAddProject = () => {
    setIsAddProjectModalOpen(true);
  };

  const handleProjectAdded = (project: Project, needsInit: boolean) => {
    console.log('[Web] Project added:', project.name, 'needs init:', needsInit);
    // "From Prompt" mode hands us an initialPrompt — switch to the new project
    // and open the Task Creation Wizard pre-filled so agents can take over.
    if (project.initialPrompt) {
      // setActiveProject must run first: the wizard targets
      // (activeProjectId || selectedProjectId), and the store helper only set
      // selectedProjectId. Without this the wizard would open against
      // whichever project tab was active before.
      setActiveProject(project.id);
      useProjectStore.getState().selectProject(project.id);
      navigate(`/p/${project.id}/kanban`);
      setTaskWizardSeed({ description: project.initialPrompt });
      setIsNewTaskDialogOpen(true);
    }
  };

  // Handler for opening inbuilt terminal with specific working directory
  const handleOpenInbuiltTerminal = useCallback((id: string, cwd: string) => {
    // Create a new terminal with the specified id and working directory
    window.API.createTerminal({
      id,
      cwd,
      cols: 80,
      rows: 24,
    });
    // Switch to terminals view to show the new terminal
    if (currentProjectId) navigate(`/p/${currentProjectId}/terminals`);
  }, [currentProjectId, navigate]);

  // Show loading screen for 2 seconds on page load
  if (isLoading) {
    return <LoadingScreen duration={2000} onComplete={handleLoadingComplete} />;
  }

  return (
    <ViewStateProvider>
      <TooltipProvider>
        <div className="flex h-screen bg-background">
          {/* Sidebar — static column on desktop, slide-in drawer on mobile */}
          <Sidebar
            onSettingsClick={() => setIsSettingsDialogOpen(true)}
            onNewTaskClick={() => setIsNewTaskDialogOpen(true)}
            onOpenOnboarding={() => setIsOnboardingOpen(true)}
            activeView={activeView}
            onViewChange={handleViewChange}
            isMobile={isMobile}
            mobileOpen={sidebarOpen}
            onMobileClose={() => setSidebarOpen(false)}
            onProjectActivate={(projectId) => {
              openProjectTab(projectId);
              navigate(`/p/${projectId}/overview`);
            }}
          />

          {/* Main content */}
          <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
            {/* Mobile header — hamburger + context + quick actions (md:hidden) */}
            <MobileTopBar
              onMenuClick={() => setSidebarOpen(true)}
              onNewTaskClick={() => setIsNewTaskDialogOpen(true)}
              onSettingsClick={() => setIsSettingsDialogOpen(true)}
              activeView={activeView}
              projectName={selectedProject?.name}
              canCreateTask={!!selectedProject?.autoBuildPath}
            />

            {/* Project Tab Bar - desktop only (mobile uses the header above) */}
            {openProjects.length > 0 && (
              <ProjectTabBar
                className="hidden md:flex"
                projects={openProjects}
                activeProjectId={activeProjectId}
                onProjectSelect={(projectId) => {
                  // Preserve the current view across project switches (unless it's
                  // a project-independent global view). The store sync effect
                  // updates activeProjectId from the new URL.
                  //const v = route.isGlobalView ? 'kanban' : activeView;
                  //navigate(`/p/${projectId}/${v}`);
                  navigate(`/p/${projectId}/overview`);
                }}
                onProjectClose={(projectId) => closeProjectTab(projectId)}
                onAddProject={handleAddProject}
                onProjectAdded={handleProjectAdded}
                onSettingsClick={() => setIsSettingsDialogOpen(true)}
                onOpenOnboarding={() => setIsOnboardingOpen(true)}
              />
            )}

            <main className="flex-1 overflow-hidden">
              {/* View content stays mounted (preserving e.g. the terminal grid)
                  but is hidden while a task page is open. */}
              <div className={cn('h-full overflow-hidden', selectedTaskId && 'hidden')}>
                {activeView === 'hermes' ? (
                  <HermesPage />
                ) : activeView === 'members' ? (
                  <MembersPage />
                ) : activeView === 'admin' ? (
                  <AdminPage />
                ) : activeView === 'transcripts' ? (
                  <TranscriptsPage />
                ) : selectedProject ? (
                  <>
                    {activeView === 'overview' && (
                      <WelcomeScreen
                        projects={projects}
                        activeProject={selectedProject}
                        onSelectProject={(projectId) => {
                          openProjectTab(projectId);
                          navigate(`/p/${projectId}/overview`);
                        }}
                      />
                    )}
                    {activeView === 'kanban' && (
                      <KanbanBoard
                        tasks={tasks}
                        onTaskClick={handleTaskClick}
                        onNewTaskClick={() => setIsNewTaskDialogOpen(true)}
                        isInitialized={!!selectedProject?.autoBuildPath}
                        onOpenUsage={() => handleViewChange('usage')}
                      />
                    )}
                    {/* TerminalGrid stays mounted but hidden to preserve xterm instances and PTY connections */}
                    <div className={activeView === 'terminals' ? 'h-full' : 'hidden'}>
                      <TerminalGrid
                        projectPath={selectedProject?.path}
                        onNewTaskClick={() => setIsNewTaskDialogOpen(true)}
                        isActive={activeView === 'terminals'}
                      />
                    </div>
                    {activeView === 'editor' && (
                      <EditorPage projectPath={selectedProject?.path} />
                    )}
                    {activeView === 'worktrees' && (
                      <Worktrees projectId={selectedProject?.id || ''} />
                    )}
                    {activeView === 'context' && (
                      <Context projectId={selectedProject?.id || ''} />
                    )}
                    {activeView === 'github-issues' && (
                      <GitHubIssues
                        onOpenSettings={() => setIsSettingsDialogOpen(true)}
                        onNavigateToTask={(taskId) => {
                          if (currentProjectId) navigate(`/p/${currentProjectId}/tasks/${taskId}`);
                        }}
                      />
                    )}
                    {activeView === 'github-prs' && (
                      <GitHubPRs
                        onOpenSettings={() => setIsSettingsDialogOpen(true)}
                        isActive={true}
                      />
                    )}
                    {activeView === 'changelog' && <Changelog />}
                    {activeView === 'usage' && (
                      <UsageView projectId={selectedProject?.id || ''} />
                    )}
                    {activeView === 'insights' && (
                      <Insights projectId={selectedProject?.id || ''} onNavigate={handleViewChange} />
                    )}
                    {activeView === 'agent-tools' && <AgentTools />}
                    {activeView === 'skills' && <SkillsPage />}
                    {activeView === 'docs' && selectedProject && (
                      <DocumentationView projectId={selectedProject.id} />
                    )}
                  </>
                ) : (
                  <WelcomeScreen
                    projects={projects}
                    activeProject={dashboardProject}
                    onSelectProject={(projectId) => {
                      openProjectTab(projectId);
                      navigate(`/p/${projectId}/kanban`);
                    }}
                  />
                )}
              </div>
              {/* Task detail — full page with its own URL (/p/:projectId/tasks/:taskId) */}
              {selectedTaskId && (
                selectedTask ? (
                  <TaskDetailPage
                    task={selectedTask}
                    onClose={handleCloseTask}
                    onSwitchToTerminals={() => handleViewChange('terminals')}
                    onOpenInbuiltTerminal={handleOpenInbuiltTerminal}
                  />
                ) : (
                  <div className="h-full flex items-center justify-center text-muted-foreground">
                    Loading task…
                  </div>
                )
              )}
            </main>
          </div>

          {/* Project Switch Loading Modal */}
          <ProjectSwitchLoadingModal
            open={isSwitchingProject}
            projectName={switchingProjectName}
          />

          {/* Toast notifications */}
          <Toaster />

          {/* Add Project Modal */}
          <AddProjectModal
            open={isAddProjectModalOpen}
            onOpenChange={setIsAddProjectModalOpen}
            onProjectAdded={handleProjectAdded}
          />

          {/* Settings Dialog */}
          <AppSettingsDialog
            open={isSettingsDialogOpen}
            onOpenChange={setIsSettingsDialogOpen}
          />

          {/* Task Creation Wizard */}
          {/*
            Fix: Use activeProjectId first, then fall back to selectedProjectId
            This ensures the correct project path is resolved in multi-tab scenarios.
            Without this, the Browse Files button wouldn't render because projectPath
            lookup would fail when the wizard is opened from a different tab than
            the one with selectedProjectId.
          */}
          {(activeProjectId || selectedProjectId) && (
            <TaskCreationWizard
              projectId={(activeProjectId || selectedProjectId)!}
              open={isNewTaskDialogOpen}
              onOpenChange={(open) => {
                setIsNewTaskDialogOpen(open);
                if (!open) setTaskWizardSeed(null);
              }}
              initialTitle={taskWizardSeed?.title}
              initialDescription={taskWizardSeed?.description}
            />
          )}

          {/* Onboarding Wizard */}
          <OnboardingWizard
            open={isOnboardingOpen}
            onOpenChange={setIsOnboardingOpen}
            onOpenTaskCreator={() => setIsNewTaskDialogOpen(true)}
            onOpenSettings={() => setIsSettingsDialogOpen(true)}
          />
        </div>
      </TooltipProvider>
    </ViewStateProvider>
  );
}

export default function App() {
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const userStatus = useAuthStore((state) => state.user?.status);
  const checkAuth = useAuthStore((state) => state.checkAuth);

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  // A registered-but-not-yet-approved account can authenticate but must not
  // reach the workspace (the backend 403s its data anyway). Hold it on the
  // waiting screen until an admin approves it.
  const isPending = isAuthenticated && userStatus === 'pending';

  return (
    <Routes>
      <Route
        path="/login"
        element={isAuthenticated ? <Navigate to="/" replace /> : <LoginPage />}
      />
      <Route
        path="/*"
        element={
          !isAuthenticated ? (
            <Navigate to="/login" replace />
          ) : isPending ? (
            <PendingApprovalScreen />
          ) : (
            <AuthenticatedApp />
          )
        }
      />
    </Routes>
  );
}
