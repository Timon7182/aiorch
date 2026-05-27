import { useState, useEffect } from 'react';
import { Eye, FileCode, ChevronDown, ChevronRight, Columns2, AlignJustify, Loader2 } from 'lucide-react';
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '../../ui/alert-dialog';
import { Badge } from '../../ui/badge';
import { Collapsible, CollapsibleTrigger, CollapsibleContent } from '../../ui/collapsible';
import { cn } from '../../../lib/utils';
import { DiffViewer } from './DiffViewer';
import { SplitDiffViewer } from './SplitDiffViewer';
import type { WorktreeDiff, WorktreeDiffFile } from '../../../shared/types';

type DiffViewMode = 'split' | 'unified';

interface DiffViewDialogProps {
  open: boolean;
  worktreeDiff: WorktreeDiff | null;
  onOpenChange: (open: boolean) => void;
}

/**
 * Single file entry with expandable diff content
 */
function FileEntry({
  file,
  isExpanded,
  onToggle,
  viewMode
}: {
  file: WorktreeDiffFile;
  isExpanded: boolean;
  onToggle: () => void;
  viewMode: DiffViewMode;
}) {
  const hasDiff = !!file.diff;

  return (
    <Collapsible
      open={isExpanded}
      onOpenChange={hasDiff ? onToggle : undefined}
    >
      <CollapsibleTrigger asChild disabled={!hasDiff}>
        <div
          className={cn(
            "flex items-center justify-between p-2 rounded-lg bg-secondary/30 transition-colors",
            hasDiff && "cursor-pointer hover:bg-secondary/50",
            !hasDiff && "cursor-default"
          )}
        >
          <div className="flex items-center gap-2 min-w-0 flex-1">
            {hasDiff ? (
              isExpanded ? (
                <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
              ) : (
                <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
              )
            ) : (
              <div className="w-4 h-4 shrink-0" /> // Spacer for alignment
            )}
            <FileCode className={cn(
              'h-4 w-4 shrink-0',
              file.status === 'added' && 'text-success',
              file.status === 'deleted' && 'text-destructive',
              file.status === 'modified' && 'text-info',
              file.status === 'renamed' && 'text-warning'
            )} />
            <span className="text-sm font-mono truncate">{file.path}</span>
          </div>
          <div className="flex items-center gap-2 shrink-0 ml-2">
            <Badge
              variant="secondary"
              className={cn(
                'text-xs',
                file.status === 'added' && 'bg-success/10 text-success',
                file.status === 'deleted' && 'bg-destructive/10 text-destructive',
                file.status === 'modified' && 'bg-info/10 text-info',
                file.status === 'renamed' && 'bg-warning/10 text-warning'
              )}
            >
              {file.status}
            </Badge>
            <span className="text-xs text-success">+{file.additions}</span>
            <span className="text-xs text-destructive">-{file.deletions}</span>
          </div>
        </div>
      </CollapsibleTrigger>
      {hasDiff && (
        <CollapsibleContent className="mt-1 ml-6">
          {viewMode === 'split' ? (
            <SplitDiffViewer diff={file.diff!} />
          ) : (
            <DiffViewer diff={file.diff!} showLineNumbers={false} />
          )}
        </CollapsibleContent>
      )}
    </Collapsible>
  );
}

/**
 * Inline panel showing the list of changed files with a Split/Unified toggle,
 * expand/collapse, and per-file expandable diffs. Used both inside the
 * DiffViewDialog (popup) and embedded directly in the task page's Changes tab.
 */
export function ChangedFilesPanel({
  worktreeDiff,
  isLoading = false,
  defaultExpanded = false,
  className,
}: {
  worktreeDiff: WorktreeDiff | null;
  isLoading?: boolean;
  /** Expand every file's diff as soon as it loads (used by the inline tab). */
  defaultExpanded?: boolean;
  className?: string;
}) {
  const [expandedFiles, setExpandedFiles] = useState<Set<number>>(new Set());
  const [viewMode, setViewMode] = useState<DiffViewMode>('split');

  // When asked to default-expand (inline Changes tab), open every file that has
  // a diff as soon as the payload arrives or changes (e.g. after Refresh), so
  // the side-by-side diffs are visible immediately instead of a bare file list.
  useEffect(() => {
    if (!defaultExpanded || !worktreeDiff?.files?.length) return;
    const withDiff = worktreeDiff.files
      .map((f, idx) => (f.diff ? idx : -1))
      .filter(idx => idx >= 0);
    if (withDiff.length > 0) {
      setExpandedFiles(new Set(withDiff));
    }
  }, [worktreeDiff, defaultExpanded]);

  const toggleFile = (idx: number) => {
    setExpandedFiles(prev => {
      const next = new Set(prev);
      if (next.has(idx)) {
        next.delete(idx);
      } else {
        next.add(idx);
      }
      return next;
    });
  };

  const expandAll = () => {
    if (!worktreeDiff?.files) return;
    const allIndices = worktreeDiff.files
      .map((_, idx) => idx)
      .filter(idx => worktreeDiff.files[idx].diff);
    setExpandedFiles(new Set(allIndices));
  };

  const collapseAll = () => {
    setExpandedFiles(new Set());
  };

  const hasAnyDiff = worktreeDiff?.files?.some(f => f.diff);
  const allExpanded = worktreeDiff?.files?.every((f, idx) => !f.diff || expandedFiles.has(idx));

  return (
    <div className={cn('flex flex-col min-h-0', className)}>
      {/* Toolbar: summary + view-mode toggle + expand/collapse */}
      <div className="flex items-center justify-between gap-2 mb-3 shrink-0">
        <span className="text-sm text-muted-foreground truncate">
          {isLoading ? 'Loading changes…' : (worktreeDiff?.summary || 'No changes found')}
        </span>
        {hasAnyDiff && (
          <div className="flex items-center gap-3 shrink-0">
            <div className="inline-flex items-center rounded-md border border-border overflow-hidden">
              <button
                onClick={() => setViewMode('split')}
                className={cn(
                  'flex items-center gap-1 px-2 py-1 text-xs transition-colors',
                  viewMode === 'split'
                    ? 'bg-primary text-primary-foreground'
                    : 'text-muted-foreground hover:bg-secondary/50'
                )}
                title="Side-by-side view"
              >
                <Columns2 className="h-3.5 w-3.5" />
                Split
              </button>
              <button
                onClick={() => setViewMode('unified')}
                className={cn(
                  'flex items-center gap-1 px-2 py-1 text-xs transition-colors',
                  viewMode === 'unified'
                    ? 'bg-primary text-primary-foreground'
                    : 'text-muted-foreground hover:bg-secondary/50'
                )}
                title="Unified (inline) view"
              >
                <AlignJustify className="h-3.5 w-3.5" />
                Unified
              </button>
            </div>
            <button
              onClick={allExpanded ? collapseAll : expandAll}
              className="text-xs text-primary hover:underline"
            >
              {allExpanded ? 'Collapse all' : 'Expand all'}
            </button>
          </div>
        )}
      </div>

      {/* File list */}
      <div className="flex-1 overflow-auto min-h-0">
        {isLoading ? (
          <div className="flex items-center justify-center gap-2 py-8 text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading changes…
          </div>
        ) : worktreeDiff?.files && worktreeDiff.files.length > 0 ? (
          <div className="space-y-2">
            {worktreeDiff.files.map((file, idx) => (
              <FileEntry
                key={idx}
                file={file}
                isExpanded={expandedFiles.has(idx)}
                onToggle={() => toggleFile(idx)}
                viewMode={viewMode}
              />
            ))}
          </div>
        ) : (
          <div className="text-center py-8 text-muted-foreground">
            No changed files found
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Dialog displaying the list of changed files with their status, line changes,
 * and expandable diff content
 */
export function DiffViewDialog({
  open,
  worktreeDiff,
  onOpenChange
}: DiffViewDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="max-w-6xl max-h-[85vh] overflow-hidden flex flex-col">
        <AlertDialogHeader>
          <AlertDialogTitle className="flex items-center gap-2">
            <Eye className="h-5 w-5 text-purple-400" />
            Changed Files
          </AlertDialogTitle>
          <AlertDialogDescription className="sr-only">
            Side-by-side and unified diff of the files changed in this task's worktree.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <ChangedFilesPanel
          worktreeDiff={worktreeDiff}
          className="flex-1 -mx-6 px-6"
        />
        <AlertDialogFooter className="mt-4">
          <AlertDialogCancel>Close</AlertDialogCancel>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
