import { useEffect, useMemo, useState } from 'react';
import {
  ChevronRight,
  Clock,
  Folder,
  FolderOpen,
  FolderPlus,
  GitBranch,
  History,
  Network,
  RefreshCw,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { get, post } from '../lib/api-client';
import { useProjectStore } from '../stores/project-store';
import type { Project } from '../shared/types';
import { Button } from './ui/button';
import { Card } from './ui/card';
import { ScrollArea } from './ui/scroll-area';
import { Separator } from './ui/separator';
import { cn } from '../lib/utils';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from './ui/select';

interface DashboardCommit {
  hash: string;
  message: string;
  author: string;
  email: string;
  date: string;
  selected: boolean;
}

interface DocsStatus {
  has_codegraph?: boolean;
  codegraph_indexing?: boolean;
  last_codegraph?: string;
}

interface BranchOption {
  name: string;
  ref: string;
  isRemote: boolean;
  isCurrent: boolean;
}

interface RepoDashboardState {
  repoKey: string;
  repoName: string;
  repoPath?: string;
  selectedBranch: string;
  selectedBranchRef: string;
  branches: BranchOption[];
  commits: DashboardCommit[];
  docsStatus: DocsStatus | null;
  codegraphAvailable: boolean;
  isLoading: boolean;
}

interface WelcomeScreenProps {
  projects: Project[];
  activeProject: Project | null;
  onNewProject: () => void;
  onOpenProject: () => void;
  onSelectProject: (projectId: string) => void;
}

export function WelcomeScreen({
  projects,
  activeProject,
  onNewProject,
  onOpenProject,
  onSelectProject
}: WelcomeScreenProps) {
  const { t } = useTranslation(['welcome', 'common']);
  const [mainBranch, setMainBranch] = useState<string | null>(activeProject?.settings.mainBranch ?? null);
  const [commits, setCommits] = useState<DashboardCommit[]>([]);
  const [docsStatus, setDocsStatus] = useState<DocsStatus | null>(null);
  const [repoDashboards, setRepoDashboards] = useState<Record<string, RepoDashboardState>>({});
  const [isLoadingDashboard, setIsLoadingDashboard] = useState(false);

  const reposByProject = useProjectStore((s) => s.reposByProject);
  const activeRepoByProject = useProjectStore((s) => s.activeRepoByProject);
  const projectRepos = activeProject ? (reposByProject[activeProject.id] ?? []) : [];
  const activeRepoPath = activeProject
    ? (projectRepos.length > 1
      ? (activeRepoByProject[activeProject.id] ?? projectRepos[0]?.path)
      : undefined)
    : undefined;

  const dashboardRepos = useMemo(() => {
    if (!activeProject) return [];

    if (projectRepos.length > 1) {
      return projectRepos.map((repo) => ({
        key: repo.path,
        name: repo.name,
        path: repo.path,
      }));
    }

    return [
      {
        key: activeProject.path,
        name: activeProject.name,
        path: undefined,
      },
    ];
  }, [activeProject, projectRepos]);

  const recentProjects = [...projects]
    .sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime())
    .slice(0, 10);

  const headerProject = useMemo(() => activeProject, [activeProject]);

  useEffect(() => {
    if (!activeProject) {
      setRepoDashboards({});
      return;
    }

    let cancelled = false;
    setIsLoadingDashboard(true);

    const loadDashboard = async () => {
      const next: Record<string, RepoDashboardState> = {};

      await Promise.all(
        dashboardRepos.map(async (repo) => {
          const repoQuery = repo.path ? `?repo=${encodeURIComponent(repo.path)}` : '';

          const branchesResult = await get<BranchOption[]>(
            `/projects/${activeProject.id}/changelog/branches${repoQuery}`,
          );

          const branches = branchesResult.success && branchesResult.data ? branchesResult.data : [];
          const preferredBranch =
            branches.find((b) => b.name === activeProject.settings.mainBranch) ??
            branches.find((b) => b.isCurrent) ??
            branches[0];

          const selectedBranch = preferredBranch?.name ?? activeProject.settings.mainBranch ?? 'main';
          const selectedBranchRef = preferredBranch?.ref ?? selectedBranch;

          const [commitsResult, docsResult, codegraphResult] = await Promise.all([
            post<DashboardCommit[]>(
              `/projects/${activeProject.id}/changelog/commits-preview`,
              {
                mode: 'git-history',
                repo: repo.path,
                branch: selectedBranchRef,
                options: {
                  type: 'last-n',
                  count: 6,
                  includeMergeCommits: true,
                },
              },
            ),
            get<DocsStatus>(
              `/projects/${activeProject.id}/docs/status${repoQuery}`,
            ),
            get<{ cgc: boolean; graphify: boolean }>(
              `/projects/${activeProject.id}/insights/code-search-availability?branch=${encodeURIComponent(selectedBranchRef)}${repo.path ? `&repo=${encodeURIComponent(repo.path)}` : ''}`,
            ),
          ]);

          next[repo.key] = {
            repoKey: repo.key,
            repoName: repo.name,
            repoPath: repo.path,
            selectedBranch,
            selectedBranchRef,
            branches,
            commits: commitsResult.success && commitsResult.data ? commitsResult.data : [],
            docsStatus: docsResult.success ? (docsResult.data ?? null) : null,
            codegraphAvailable: !!(codegraphResult.success && codegraphResult.data?.cgc),
            isLoading: false,
          };
        }),
      );

      if (cancelled) return;
      setRepoDashboards(next);
      setIsLoadingDashboard(false);
    };

    loadDashboard().catch(() => {
      if (!cancelled) {
        setRepoDashboards({});
        setIsLoadingDashboard(false);
      }
    });

    return () => {
      cancelled = true;
    };
  }, [activeProject, dashboardRepos]);

  const formatRelativeTime = (date: Date) => {
    const now = new Date();
    const diffMs = now.getTime() - new Date(date).getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMins / 60);
    const diffDays = Math.floor(diffHours / 24);

    if (diffMins < 1) return t('common:time.justNow');
    if (diffMins < 60) return t('common:time.minutesAgo', { count: diffMins });
    if (diffHours < 24) return t('common:time.hoursAgo', { count: diffHours });
    if (diffDays < 7) return t('common:time.daysAgo', { count: diffDays });
    return new Date(date).toLocaleDateString();
  };

  const formatTimestamp = (value?: string | null) => {
    if (!value) return t('welcome:dashboard.codegraphNever');
    return new Date(value).toLocaleString();
  };

  const handleBranchChange = async (repoKey: string, branchRef: string) => {
    if (!activeProject) return;
    const projectId = activeProject.id;

    const repoState = repoDashboards[repoKey];
    if (!repoState) return;

    const branchMeta = repoState.branches.find((b) => b.ref === branchRef || b.name === branchRef);
    const selectedBranch = branchMeta?.name ?? branchRef;
    const selectedBranchRef = branchMeta?.ref ?? branchRef;

    setRepoDashboards((prev) => ({
      ...prev,
      [repoKey]: {
        ...prev[repoKey],
        selectedBranch,
        selectedBranchRef,
        isLoading: true,
      },
    }));

    const repoQuery = repoState.repoPath ? `?repo=${encodeURIComponent(repoState.repoPath)}` : '';

    const [commitsResult, docsResult, codegraphResult] = await Promise.all([
      post<DashboardCommit[]>(
        `/projects/${projectId}/changelog/commits-preview`,
        {
          mode: 'git-history',
          repo: repoState.repoPath,
          branch: selectedBranchRef,
          options: {
            type: 'last-n',
            count: 6,
            includeMergeCommits: true,
          },
        },
      ),
      get<DocsStatus>(`/projects/${projectId}/docs/status${repoQuery}`),
      get<{ cgc: boolean; graphify: boolean }>(
        `/projects/${projectId}/insights/code-search-availability?branch=${encodeURIComponent(selectedBranchRef)}${repoState.repoPath ? `&repo=${encodeURIComponent(repoState.repoPath)}` : ''}`,
      ),
    ]);

    setRepoDashboards((prev) => ({
      ...prev,
      [repoKey]: {
        ...prev[repoKey],
        selectedBranch,
        selectedBranchRef,
        commits: commitsResult.success && commitsResult.data ? commitsResult.data : [],
        docsStatus: docsResult.success ? (docsResult.data ?? null) : null,
        codegraphAvailable: !!(codegraphResult.success && codegraphResult.data?.cgc),
        isLoading: false,
      },
    }));
  };

  return (
    <div className="flex h-full items-start justify-center overflow-auto p-8">
      <div className="w-full max-w-6xl">
        <div className="mb-10 text-center">
          <h1 className="text-3xl font-bold tracking-tight text-foreground">
            {activeProject ? t('welcome:dashboard.title') : t('welcome:hero.title')}
          </h1>
          <p className="mt-3 text-muted-foreground">
            {activeProject ? t('welcome:dashboard.subtitle') : t('welcome:hero.subtitle')}
          </p>
        </div>

        <div className="mb-10 flex justify-center gap-4">
          <Button
            size="lg"
            onClick={onNewProject}
            className="gap-2 px-6"
          >
            <FolderPlus className="h-5 w-5" />
            {t('welcome:actions.newProject')}
          </Button>
          <Button
            size="lg"
            variant="secondary"
            onClick={onOpenProject}
            className="gap-2 px-6"
          >
            <FolderOpen className="h-5 w-5" />
            {t('welcome:actions.openProject')}
          </Button>
        </div>

        {headerProject && (
          <div className="mb-8 space-y-6">
            <div
              className={cn(
                'grid gap-6',
                dashboardRepos.length > 1 ? 'xl:grid-cols-2' : 'grid-cols-1',
              )}
            >
              {dashboardRepos.map((repo) => {
                const state = repoDashboards[repo.key];
                //const branchLabel = state?.selectedBranch ?? activeProject.settings.mainBranch ?? 'main';
                const branchLabel = state?.selectedBranch ?? headerProject.settings.mainBranch ?? 'main';

                return (
                  <div key={repo.key} className="space-y-6">
                    <Card className="border border-border bg-card/70 p-6 backdrop-blur-sm">
                      <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
                        <div className="min-w-0">
                          <p className="text-sm font-medium uppercase tracking-[0.18em] text-muted-foreground">
                            {t('welcome:dashboard.activeProject')}
                          </p>
                          <h2 className="mt-2 truncate text-3xl font-semibold text-foreground">
                            {headerProject.name}
                          </h2>
                          <p className="mt-2 truncate text-sm text-muted-foreground">
                            {headerProject.path}
                          </p>
                          {repo.path && (
                            <p className="mt-3 text-xs uppercase tracking-[0.16em] text-primary">
                              Repo: {repo.name}
                            </p>
                          )}
                        </div>

                        <div className="w-full max-w-xs">
                          <p className="mb-2 text-xs uppercase tracking-[0.16em] text-muted-foreground">
                            {t('welcome:dashboard.mainBranch')}
                          </p>
                          <Select
                            value={state?.selectedBranchRef ?? branchLabel}
                            onValueChange={(value) => handleBranchChange(repo.key, value)}
                          >
                            <SelectTrigger className="h-10 bg-background/70">
                              <SelectValue placeholder={branchLabel} />
                            </SelectTrigger>
                            <SelectContent>
                              {(state?.branches ?? []).map((branch) => (
                                <SelectItem key={branch.ref} value={branch.ref}>
                                  {branch.name}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                      </div>
                    </Card>

                    <div className="grid gap-6 lg:grid-cols-2">
                      <Card className="border border-border bg-card/70 backdrop-blur-sm">
                        <div className="flex items-center justify-between p-5 pb-3">
                          <div className="flex items-center gap-2">
                            <History className="h-4 w-4 text-primary" />
                            <h3 className="text-sm font-semibold text-foreground">
                              {t('welcome:dashboard.branchFreshness')}
                            </h3>
                          </div>
                          {(isLoadingDashboard || state?.isLoading) && (
                            <RefreshCw className="h-4 w-4 animate-spin text-muted-foreground" />
                          )}
                        </div>
                        <Separator />
                        <div className="p-3">
                          {(state?.commits?.length ?? 0) > 0 ? (
                            <div className="space-y-2">
                              {state!.commits.map((commit) => (
                                <div key={`${commit.hash}-${commit.date}`} className="rounded-xl border border-border/60 bg-background/60 p-3">
                                  <div className="flex items-start justify-between gap-3">
                                    <div className="min-w-0">
                                      <p className="truncate text-sm font-medium text-foreground">
                                        {commit.message}
                                      </p>
                                      <p className="mt-1 text-xs text-muted-foreground">
                                        {commit.author} · {new Date(commit.date).toLocaleString()}
                                      </p>
                                    </div>
                                    <span className="shrink-0 rounded-md bg-primary/10 px-2 py-1 font-mono text-xs text-primary">
                                      {commit.hash.slice(0, 7)}
                                    </span>
                                  </div>
                                </div>
                              ))}
                            </div>
                          ) : (
                            <div className="rounded-xl border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
                              {t('welcome:dashboard.noCommits')}
                            </div>
                          )}
                        </div>
                      </Card>

                      <Card className="border border-border bg-card/70 backdrop-blur-sm">
                        <div className="flex items-center justify-between p-5 pb-3">
                          <div className="flex items-center gap-2">
                            <Network className="h-4 w-4 text-primary" />
                            <h3 className="text-sm font-semibold text-foreground">
                              {t('welcome:dashboard.codegraphStatus')}
                            </h3>
                          </div>
                          {(isLoadingDashboard || state?.isLoading || state?.docsStatus?.codegraph_indexing) && (
                            <RefreshCw className="h-4 w-4 animate-spin text-muted-foreground" />
                          )}
                        </div>
                        <Separator />
                        <div className="space-y-4 p-5">
                          <div className="rounded-xl border border-border/60 bg-background/60 p-4">
                            <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">
                              {t('welcome:dashboard.lastCodegraphUpdate')}
                            </p>
                            <p className="mt-2 text-lg font-semibold text-foreground">
                              {formatTimestamp(state?.docsStatus?.last_codegraph)}
                            </p>
                          </div>
                          <div className="rounded-xl border border-border/60 bg-background/60 p-4">
                            <p className="text-xs uppercase tracking-[0.16em] text-muted-foreground">
                              {t('welcome:dashboard.codegraphState')}
                            </p>
                            <p className="mt-2 text-sm font-medium text-foreground">
                              {state?.docsStatus?.codegraph_indexing
                                ? t('welcome:dashboard.codegraphIndexing')
                                : state?.codegraphAvailable
                                  ? t('welcome:dashboard.codegraphReady')
                                  : t('welcome:dashboard.codegraphMissing')}
                            </p>
                          </div>
                        </div>
                      </Card>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {!activeProject && recentProjects.length > 0 && (
          <Card className="border border-border bg-card/50 backdrop-blur-sm">
            <div className="p-4 pb-3">
              <div className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
                <Clock className="h-4 w-4" />
                {t('welcome:recentProjects.title')}
              </div>
            </div>
            <Separator />
            <ScrollArea className="max-h-[320px]">
              <div className="p-2">
                {recentProjects.map((project) => (
                  <button
                    key={project.id}
                    onClick={() => onSelectProject(project.id)}
                    className="group flex w-full items-center gap-3 rounded-lg px-3 py-3 text-left transition-colors hover:bg-accent/50"
                  >
                    <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-accent/20 text-accent-foreground">
                      <Folder className="h-5 w-5" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="truncate font-medium text-foreground">
                          {project.name}
                        </span>
                        {project.autoBuildPath && (
                          <span className="shrink-0 rounded-full bg-success/20 px-1.5 py-0.5 text-[10px] text-success">
                            Initialized
                          </span>
                        )}
                      </div>
                      <p className="mt-0.5 truncate text-xs text-muted-foreground">
                        {project.path}
                      </p>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <span className="text-xs text-muted-foreground">
                        {formatRelativeTime(project.updatedAt)}
                      </span>
                      <ChevronRight className="h-4 w-4 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
                    </div>
                  </button>
                ))}
              </div>
            </ScrollArea>
          </Card>
        )}

        {projects.length === 0 && (
          <Card className="border border-dashed border-border bg-card/30 p-8 text-center">
            <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-accent/20">
              <Folder className="h-6 w-6 text-accent-foreground" />
            </div>
            <h3 className="mb-1 font-medium text-foreground">{t('welcome:recentProjects.empty')}</h3>
            <p className="mb-4 text-sm text-muted-foreground">
              {t('welcome:recentProjects.emptyDescription')}
            </p>
          </Card>
        )}
      </div>
    </div>
  );
}