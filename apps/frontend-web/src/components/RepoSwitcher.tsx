import { GitBranch, ChevronDown, Check } from 'lucide-react';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from './ui/popover';
import { Separator } from './ui/separator';
import { cn } from '../lib/utils';
import { useProjectStore, ALL_REPOS } from '../stores/project-store';

interface RepoSwitcherProps {
  projectId: string;
  className?: string;
}

/**
 * Scopes the insights chat for a multi-repo project. The default is "All
 * repositories" — the chat grounds in the whole project root so the assistant
 * can read/search every repo plus the generated docs and decide where to look.
 * Picking a single repo narrows the chat (and enables that repo's CodeGraph).
 * Renders nothing for single-repo projects (the project root is the one repo).
 */
export function RepoSwitcher({ projectId, className }: RepoSwitcherProps) {
  const reposByProject = useProjectStore((s) => s.reposByProject);
  const chatGroundByProject = useProjectStore((s) => s.chatGroundByProject);
  const setChatGround = useProjectStore((s) => s.setChatGround);

  const repos = reposByProject[projectId] ?? [];
  if (repos.length <= 1) return null;

  const ground = chatGroundByProject[projectId] ?? ALL_REPOS;
  const isAll = ground === ALL_REPOS;
  const activeRepo = repos.find((r) => r.path === ground);
  const triggerLabel = isAll ? 'All repos' : (activeRepo?.name ?? 'Select');

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(
            'flex items-center gap-2 rounded-lg border border-border/60 bg-accent/30 px-3 py-1.5 text-left hover:bg-accent/50 transition-colors',
            className
          )}
          title={isAll ? 'Whole project (all repositories)' : activeRepo?.path}
        >
          <GitBranch className="h-3.5 w-3.5 shrink-0 text-primary" />
          <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Repo
          </span>
          <span className="max-w-[10rem] truncate text-xs font-semibold">
            {triggerLabel}
          </span>
          <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-72 p-1" align="end">
        <div className="px-2 py-1.5 text-xs font-semibold text-muted-foreground">
          Chat scope ({repos.length} repos)
        </div>
        <Separator className="my-1" />
        <button
          type="button"
          onClick={() => setChatGround(projectId, ALL_REPOS)}
          className={cn(
            'flex w-full items-center gap-2 rounded-md px-2 py-2 text-left text-sm hover:bg-accent/50',
            isAll && 'bg-accent'
          )}
        >
          <div className="flex h-4 w-4 shrink-0 items-center justify-center">
            {isAll && <Check className="h-3.5 w-3.5" />}
          </div>
          <div className="min-w-0 flex-1">
            <div className="truncate font-medium">All repositories</div>
            <div className="truncate text-[10px] text-muted-foreground/60">
              Whole project — assistant decides where to look
            </div>
          </div>
        </button>
        <Separator className="my-1" />
        {repos.map((repo) => {
          const isActive = !isAll && repo.path === ground;
          return (
            <button
              key={repo.path}
              type="button"
              onClick={() => setChatGround(projectId, repo.path)}
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
  );
}
