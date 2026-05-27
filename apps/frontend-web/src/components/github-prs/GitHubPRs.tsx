import { useCallback, useEffect, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import { GitPullRequest, RefreshCw, ExternalLink, Settings } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useProjectStore } from '../../stores/project-store';
import { useGitHubPRs, usePRFiltering } from './hooks';
import { PRList, PRDetail, PRFilterBar } from './components';
import { Button } from '../ui/button';
import { ResizablePanels } from '../ui/resizable-panels';

interface GitHubPRsProps {
  onOpenSettings?: () => void;
  isActive?: boolean;
}

function NotConnectedState({
  error,
  onOpenSettings,
  t
}: {
  error: string | null;
  onOpenSettings?: () => void;
  t: (key: string) => string;
}) {
  return (
    <div className="flex-1 flex items-center justify-center p-8">
      <div className="text-center max-w-md">
        <GitPullRequest className="h-12 w-12 mx-auto mb-4 text-muted-foreground opacity-50" />
        <h3 className="text-lg font-medium mb-2">{t('prReview.notConnected')}</h3>
        <p className="text-sm text-muted-foreground mb-4">
          {error || t('prReview.connectPrompt')}
        </p>
        {onOpenSettings && (
          <Button onClick={onOpenSettings} variant="outline">
            <Settings className="h-4 w-4 mr-2" />
            {t('prReview.openSettings')}
          </Button>
        )}
      </div>
    </div>
  );
}

function EmptyState({ message }: { message: string }) {
  return (
    <div className="flex-1 flex items-center justify-center">
      <div className="text-center text-muted-foreground">
        <GitPullRequest className="h-8 w-8 mx-auto mb-2 opacity-50" />
        <p>{message}</p>
      </div>
    </div>
  );
}

export function GitHubPRs({ onOpenSettings, isActive = false }: GitHubPRsProps) {
  const { t } = useTranslation('common');
  const projects = useProjectStore((state) => state.projects);
  const selectedProjectId = useProjectStore((state) => state.selectedProjectId);
  const selectedProject = projects.find((p) => p.id === selectedProjectId);

  const {
    prs,
    isLoading,
    isInitialLoading,
    error,
    selectedPRNumber,
    reviewResult,
    reviewProgress,
    isReviewing,
    selectPR,
    runReview,
    runFollowupReview,
    checkNewCommits,
    cancelReview,
    postReview,
    postComment,
    approvePR,
    mergePR,
    assignPR,
    refresh,
    isConnected,
    repoFullName,
    getReviewStateForPR,
  } = useGitHubPRs(selectedProject?.id);

  const selectedPR = prs.find(pr => pr.number === selectedPRNumber);

  // Selected PR is reflected in the URL (?pr=<number>) so it can be linked.
  const [searchParams, setSearchParams] = useSearchParams();
  const prParam = searchParams.get('pr');
  const didApplyUrlPr = useRef(false);

  useEffect(() => {
    if (didApplyUrlPr.current) return;
    if (!prParam) { didApplyUrlPr.current = true; return; }
    const num = parseInt(prParam, 10);
    if (Number.isNaN(num)) { didApplyUrlPr.current = true; return; }
    if (prs.length === 0) return; // wait for PRs to load
    if (selectedPRNumber !== num && prs.some((p) => p.number === num)) {
      selectPR(num);
    }
    didApplyUrlPr.current = true;
  }, [prParam, prs, selectedPRNumber, selectPR]);

  useEffect(() => {
    if (!didApplyUrlPr.current) return;
    const current = searchParams.get('pr');
    if (selectedPRNumber != null && String(selectedPRNumber) !== current) {
      const next = new URLSearchParams(searchParams);
      next.set('pr', String(selectedPRNumber));
      setSearchParams(next, { replace: true });
    } else if (selectedPRNumber == null && current) {
      const next = new URLSearchParams(searchParams);
      next.delete('pr');
      setSearchParams(next, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedPRNumber]);

  // Get previousResult and newCommitsCheck for follow-up review continuity
  const selectedPRReviewState = selectedPRNumber
    ? getReviewStateForPR(selectedPRNumber)
    : null;
  const previousReviewResult = selectedPRReviewState?.previousResult ?? null;
  const storedNewCommitsCheck = selectedPRReviewState?.newCommitsCheck ?? null;

  // PR filtering
  const {
    filteredPRs,
    contributors,
    filters,
    setSearchQuery,
    setContributors,
    setStatuses,
    clearFilters,
    hasActiveFilters,
  } = usePRFiltering(prs, getReviewStateForPR);

  const handleRunReview = useCallback(() => {
    if (selectedPRNumber) {
      runReview(selectedPRNumber);
    }
  }, [selectedPRNumber, runReview]);

  const handleRunFollowupReview = useCallback(() => {
    if (selectedPRNumber) {
      runFollowupReview(selectedPRNumber);
    }
  }, [selectedPRNumber, runFollowupReview]);

  const handleCheckNewCommits = useCallback(async () => {
    if (selectedPRNumber) {
      return await checkNewCommits(selectedPRNumber);
    }
    return { hasNewCommits: false, newCommitCount: 0 };
  }, [selectedPRNumber, checkNewCommits]);

  const handleCancelReview = useCallback(() => {
    if (selectedPRNumber) {
      cancelReview(selectedPRNumber);
    }
  }, [selectedPRNumber, cancelReview]);

  const handlePostReview = useCallback(async (selectedFindingIds?: string[]): Promise<boolean> => {
    if (selectedPRNumber && reviewResult) {
      return await postReview(selectedPRNumber, selectedFindingIds);
    }
    return false;
  }, [selectedPRNumber, reviewResult, postReview]);

  const handlePostComment = useCallback(async (body: string) => {
    if (selectedPRNumber) {
      await postComment(selectedPRNumber, body);
    }
  }, [selectedPRNumber, postComment]);

  const handleApprovePR = useCallback(async (body: string): Promise<boolean> => {
    if (selectedPRNumber) {
      return await approvePR(selectedPRNumber, body);
    }
    return false;
  }, [selectedPRNumber, approvePR]);

  const handleMergePR = useCallback(async (mergeMethod?: 'merge' | 'squash' | 'rebase') => {
    if (selectedPRNumber) {
      await mergePR(selectedPRNumber, mergeMethod);
    }
  }, [selectedPRNumber, mergePR]);

  const handleAssignPR = useCallback(async (username: string) => {
    if (selectedPRNumber) {
      await assignPR(selectedPRNumber, username);
    }
  }, [selectedPRNumber, assignPR]);

  const handleGetLogs = useCallback(async () => {
    if (selectedProjectId && selectedPRNumber) {
      return await window.API.github.getPRLogs(selectedProjectId, selectedPRNumber);
    }
    return null;
  }, [selectedProjectId, selectedPRNumber]);

  // Initial loading state - show while first connection check is in progress
  if (isInitialLoading) {
    return (
      <div className="flex-1 flex items-center justify-center p-8">
        <div className="text-center">
          <GitPullRequest className="h-8 w-8 mx-auto mb-3 text-muted-foreground animate-pulse" />
          <p className="text-sm text-muted-foreground">{t('prReview.connectingToGitHub')}</p>
        </div>
      </div>
    );
  }

  // Not connected state
  if (!isConnected) {
    return <NotConnectedState error={error} onOpenSettings={onOpenSettings} t={t} />;
  }

  return (
    <div className="flex-1 flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <div className="flex items-center gap-3">
          <h2 className="text-sm font-medium flex items-center gap-2">
            <GitPullRequest className="h-4 w-4" />
            {t('prReview.pullRequests')}
          </h2>
          {repoFullName && (
            <a
              href={`https://github.com/${repoFullName}/pulls`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1"
            >
              {repoFullName}
              <ExternalLink className="h-3 w-3" />
            </a>
          )}
          <span className="text-xs text-muted-foreground">
            {prs.length} {t('prReview.open')}
          </span>
        </div>
        <Button
          variant="ghost"
          size="icon"
          onClick={refresh}
          disabled={isLoading}
        >
          <RefreshCw className={`h-4 w-4 ${isLoading ? 'animate-spin' : ''}`} />
        </Button>
      </div>

      {/* Content - Resizable split panels */}
      <ResizablePanels
        defaultLeftWidth={50}
        minLeftWidth={30}
        maxLeftWidth={70}
        storageKey="github-prs-panel-width"
        leftPanel={
          <div className="flex flex-col h-full">
            <PRFilterBar
              filters={filters}
              contributors={contributors}
              hasActiveFilters={hasActiveFilters}
              onSearchChange={setSearchQuery}
              onContributorsChange={setContributors}
              onStatusesChange={setStatuses}
              onClearFilters={clearFilters}
            />
            <PRList
              prs={filteredPRs}
              selectedPRNumber={selectedPRNumber}
              isLoading={isLoading}
              error={error}
              getReviewStateForPR={getReviewStateForPR}
              onSelectPR={selectPR}
            />
          </div>
        }
        rightPanel={
          selectedPR ? (
            <PRDetail
              pr={selectedPR}
              reviewResult={reviewResult}
              previousReviewResult={previousReviewResult}
              reviewProgress={reviewProgress}
              isReviewing={isReviewing}
              initialNewCommitsCheck={storedNewCommitsCheck}
              isActive={isActive}
              onRunReview={handleRunReview}
              onRunFollowupReview={handleRunFollowupReview}
              onCheckNewCommits={handleCheckNewCommits}
              onCancelReview={handleCancelReview}
              onPostReview={handlePostReview}
              onPostComment={handlePostComment}
              onApprovePR={handleApprovePR}
              onMergePR={handleMergePR}
              onAssignPR={handleAssignPR}
              onGetLogs={handleGetLogs}
            />
          ) : (
            <EmptyState message={t('prReview.selectPRToView')} />
          )
        }
      />
    </div>
  );
}
