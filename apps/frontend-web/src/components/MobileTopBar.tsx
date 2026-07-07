import { Menu, Plus, Settings, Sun, Moon } from 'lucide-react';
import { Button } from './ui/button';
import { useSettingsStore, saveSettings } from '../stores/settings-store';
import type { SidebarView } from './Sidebar';

interface MobileTopBarProps {
  /** Open the navigation drawer. */
  onMenuClick: () => void;
  onNewTaskClick: () => void;
  onSettingsClick: () => void;
  /** Active view — used to label the bar so users know where they are. */
  activeView: SidebarView;
  /** Currently selected project name (shown when present). */
  projectName?: string;
  /** Whether a "New Task" action makes sense for the current project. */
  canCreateTask?: boolean;
}

// Human-readable titles for each view. Kept local so the bar stays
// self-contained and never shows a raw view id.
const VIEW_TITLES: Record<SidebarView, string> = {
  overview: 'Overview',
  kanban: 'Tasks',
  terminals: 'Terminals',
  editor: 'Editor',
  context: 'Context',
  'github-issues': 'Issues',
  'github-prs': 'Pull Requests',
  changelog: 'Changelog',
  insights: 'Chat',
  worktrees: 'Worktrees',
  'agent-tools': 'Agent Tools',
  skills: 'Skills',
  hermes: 'Hermes',
  members: 'Members',
  transcripts: 'Transcripts',
  docs: 'Docs',
  usage: 'Usage',
  admin: 'Administration',
};

export function MobileTopBar({
  onMenuClick,
  onNewTaskClick,
  onSettingsClick,
  activeView,
  projectName,
  canCreateTask = false,
}: MobileTopBarProps) {
  const theme = useSettingsStore((state) => state.settings.theme);
  const updateStoreSettings = useSettingsStore((state) => state.updateSettings);
  const isDark =
    theme === 'dark' ||
    (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);

  const toggleTheme = () => {
    const newTheme = isDark ? 'light' : 'dark';
    updateStoreSettings({ theme: newTheme });
    saveSettings({ theme: newTheme });
  };

  return (
    <header className="flex h-14 shrink-0 items-center gap-1 border-b border-border bg-card px-2 md:hidden">
      <Button
        variant="ghost"
        size="icon"
        className="h-10 w-10 shrink-0"
        onClick={onMenuClick}
        aria-label="Open menu"
      >
        <Menu className="h-5 w-5" />
      </Button>

      <div className="flex min-w-0 flex-1 flex-col leading-tight">
        <span className="truncate text-sm font-semibold">{VIEW_TITLES[activeView]}</span>
        {projectName && (
          <span className="truncate text-[11px] text-muted-foreground">{projectName}</span>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-0.5">
        <Button
          variant="ghost"
          size="icon"
          className="h-10 w-10"
          onClick={toggleTheme}
          aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
        >
          {isDark ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="h-10 w-10"
          onClick={onSettingsClick}
          aria-label="Settings"
        >
          <Settings className="h-5 w-5" />
        </Button>
        {canCreateTask && (
          <Button
            size="icon"
            className="h-10 w-10"
            onClick={onNewTaskClick}
            aria-label="New task"
          >
            <Plus className="h-5 w-5" />
          </Button>
        )}
      </div>
    </header>
  );
}
