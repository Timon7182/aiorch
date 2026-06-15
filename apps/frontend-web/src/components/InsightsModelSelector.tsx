import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { Sliders, Check, Loader2, Database } from 'lucide-react';
import { Button } from './ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  DropdownMenuLabel
} from './ui/dropdown-menu';
import { PROVIDER_INFO } from '../shared/constants';
import type { InsightsModelConfig, InsightsProvider, CodeSearchBackend, DatabaseProfileSummary } from '../shared/types';
import { CustomModelModal } from './CustomModelModal';
import { useInsightsStore, loadInsightsProviders } from '../stores/insights-store';

interface InsightsModelSelectorProps {
  projectId: string;
  currentConfig?: InsightsModelConfig;
  onConfigChange: (config: InsightsModelConfig) => void;
  disabled?: boolean;
  // Whether CodeGraph is indexed for the current branch/repo scope. When false,
  // the CodeGraph option is shown disabled ("not indexed for this branch").
  cgcAvailable?: boolean;
}

export function InsightsModelSelector({
  projectId,
  currentConfig,
  onConfigChange,
  disabled,
  cgcAvailable = true
}: InsightsModelSelectorProps) {
  const { t } = useTranslation(['common', 'dialogs']);
  const [showCustomModal, setShowCustomModal] = useState(false);
  const availableProviders = useInsightsStore((s) => s.availableProviders);
  const isLoadingProviders = useInsightsStore((s) => s.isLoadingProviders);

  // Load providers on mount and refresh every 30s
  useEffect(() => {
    loadInsightsProviders(projectId);
    const interval = setInterval(() => loadInsightsProviders(projectId), 30000);
    return () => clearInterval(interval);
  }, [projectId]);

  // Registered DB connections (for the optional chat→DB connection).
  const [databases, setDatabases] = useState<DatabaseProfileSummary[]>([]);
  useEffect(() => {
    let active = true;
    window.API.listDatabases?.()
      .then((res) => { if (active && res?.success && res.data) setDatabases(res.data); })
      .catch(() => {});
    return () => { active = false; };
  }, []);

  const currentProvider = currentConfig?.provider || 'claude';

  const handleSelectProviderModel = useCallback((provider: InsightsProvider, modelId: string, modelLabel: string) => {
    onConfigChange({
      provider,
      profileId: 'custom',
      model: modelId,
      thinkingLevel: provider === 'claude' ? 'medium' : undefined,
    });
  }, [onConfigChange]);

  const handleCustomSave = useCallback((config: InsightsModelConfig) => {
    onConfigChange(config);
    setShowCustomModal(false);
  }, [onConfigChange]);

  // Switch the code-search backend while preserving the current provider/model.
  const handleSelectCodeSearch = useCallback((codeSearch: CodeSearchBackend) => {
    const base: InsightsModelConfig = currentConfig ?? {
      provider: 'claude',
      profileId: 'custom',
      model: 'sonnet',
      thinkingLevel: 'medium',
    };
    onConfigChange({ ...base, codeSearch });
  }, [onConfigChange, currentConfig]);

  const currentCodeSearch: CodeSearchBackend = currentConfig?.codeSearch ?? 'auto';
  const codeSearchOptions: CodeSearchBackend[] = ['auto', 'cgc', 'files'];

  // Connect the chat to a registered DB (read-only), preserving provider/model.
  const handleSelectDb = useCallback((dbProfileId: string | null) => {
    const base: InsightsModelConfig = currentConfig ?? {
      provider: 'claude', profileId: 'custom', model: 'sonnet', thinkingLevel: 'medium',
    };
    onConfigChange({ ...base, dbProfileId: dbProfileId ?? undefined });
  }, [onConfigChange, currentConfig]);
  const currentDb = currentConfig?.dbProfileId ?? null;

  // Build display text
  const getDisplayText = () => {
    if (currentConfig?.model) {
      const providerName = PROVIDER_INFO[currentProvider]?.displayName || currentProvider;
      // Find the model label from available providers
      const providerData = availableProviders.find(p => p.provider === currentProvider);
      const modelLabel = providerData?.models.find(m => m.id === currentConfig.model)?.label || currentConfig.model;
      return `${providerName}: ${modelLabel}`;
    }
    return t('common:insights.modelSelector.selectModel', 'Select model');
  };

  const otherProviders = availableProviders.filter(
    (p) => p.available && p.models.length > 0
  );

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            className="h-8 gap-2 px-2"
            disabled={disabled}
            title={`Model: ${getDisplayText()}`}
          >
            <Sliders className="h-4 w-4" />
            <span className="hidden text-xs text-muted-foreground sm:inline">
              {getDisplayText()}
            </span>
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-72">
          {isLoadingProviders && (
            <div className="flex items-center gap-2 px-2 py-1.5 text-xs text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              {t('common:insights.modelSelector.detecting', 'Detecting providers...')}
            </div>
          )}

          {otherProviders.length > 0 && (
            <>
              <DropdownMenuLabel>{t('common:insights.modelSelector.providers', 'Providers')}</DropdownMenuLabel>
              {otherProviders.map((provider) => (
                <div key={provider.provider}>
                  <div className="px-2 py-1 text-xs font-medium text-muted-foreground">
                    {provider.displayName}
                  </div>
                  {provider.models.slice(0, 5).map((model) => {
                    const isSelected = currentProvider === provider.provider
                      && currentConfig?.model === model.id;
                    return (
                      <DropdownMenuItem
                        key={`${provider.provider}-${model.id}`}
                        onClick={() => handleSelectProviderModel(
                          provider.provider as InsightsProvider,
                          model.id,
                          model.label
                        )}
                        className="flex cursor-pointer items-center gap-2 pl-4"
                      >
                        <div className="min-w-0 flex-1">
                          <div className="text-sm">{model.label}</div>
                        </div>
                        {isSelected && (
                          <Check className="h-4 w-4 shrink-0 text-primary" />
                        )}
                      </DropdownMenuItem>
                    );
                  })}
                </div>
              ))}
            </>
          )}

          {/* Code search backend */}
          <DropdownMenuSeparator />
          <DropdownMenuLabel>
            {t('common:insights.modelSelector.codeSearch.label', 'Code search')}
          </DropdownMenuLabel>
          {codeSearchOptions.map((opt) => {
            const isSelected = currentCodeSearch === opt;
            const isUnavailable = opt === 'cgc' && !cgcAvailable;
            return (
              <DropdownMenuItem
                key={opt}
                disabled={isUnavailable}
                onClick={() => !isUnavailable && handleSelectCodeSearch(opt)}
                className="flex cursor-pointer items-center gap-2 pl-4"
              >
                <div className="min-w-0 flex-1">
                  <div className="text-sm">
                    {t(`common:insights.modelSelector.codeSearch.${opt}`, opt)}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {isUnavailable
                      ? t('common:insights.modelSelector.codeSearch.cgcUnavailable', 'Not indexed for this branch')
                      : t(`common:insights.modelSelector.codeSearch.${opt}Desc`, '')}
                  </div>
                </div>
                {isSelected && <Check className="h-4 w-4 shrink-0 text-primary" />}
              </DropdownMenuItem>
            );
          })}

          {/* Database connection (read-only) */}
          <DropdownMenuSeparator />
          <DropdownMenuLabel className="flex items-center gap-1.5">
            <Database className="h-3.5 w-3.5" />
            {t('common:insights.modelSelector.database.label', 'Database')}
          </DropdownMenuLabel>
          <DropdownMenuItem
            onClick={() => handleSelectDb(null)}
            className="flex cursor-pointer items-center gap-2 pl-4"
          >
            <div className="min-w-0 flex-1">
              <div className="text-sm">{t('common:insights.modelSelector.database.none', 'None')}</div>
              <div className="text-xs text-muted-foreground">
                {t('common:insights.modelSelector.database.noneDesc', 'No database connection')}
              </div>
            </div>
            {currentDb === null && <Check className="h-4 w-4 shrink-0 text-primary" />}
          </DropdownMenuItem>
          {databases.map((db) => (
            <DropdownMenuItem
              key={db.id}
              onClick={() => handleSelectDb(db.id)}
              className="flex cursor-pointer items-center gap-2 pl-4"
            >
              <div className="min-w-0 flex-1">
                <div className="text-sm">{db.name}</div>
                <div className="text-xs text-muted-foreground">
                  {db.kind}{db.database ? ` · ${db.database}` : ''}{db.env ? ` · ${db.env}` : ''}
                </div>
              </div>
              {currentDb === db.id && <Check className="h-4 w-4 shrink-0 text-primary" />}
            </DropdownMenuItem>
          ))}

          {/* Custom */}
          <DropdownMenuSeparator />
          <DropdownMenuItem
            onClick={() => setShowCustomModal(true)}
            className="flex cursor-pointer items-center gap-2"
          >
            <Sliders className="h-4 w-4 shrink-0" />
            <div className="flex-1">
              <div className="font-medium">{t('common:insights.modelSelector.custom', 'Custom...')}</div>
              <div className="text-xs text-muted-foreground">
                {t('dialogs:customModel.description', 'Choose model & thinking level')}
              </div>
            </div>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <CustomModelModal
        open={showCustomModal}
        currentConfig={currentConfig}
        availableProviders={availableProviders}
        onSave={handleCustomSave}
        onClose={() => setShowCustomModal(false)}
      />
    </>
  );
}
