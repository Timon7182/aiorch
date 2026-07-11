import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Send, ScrollText, Info } from 'lucide-react';
import { Input } from '../../ui/input';
import { Label } from '../../ui/label';
import { Switch } from '../../ui/switch';
import { Separator } from '../../ui/separator';
import { PasswordInput } from '../../project-settings/PasswordInput';
import type { ProjectEnvConfig } from '../../../shared/types';

interface TelegramIntegrationProps {
  envConfig: ProjectEnvConfig | null;
  updateEnvConfig: (updates: Partial<ProjectEnvConfig>) => void;
}

/**
 * Telegram bot bridge + Graylog reader settings.
 *
 * Binds a Telegram alert chat to this project: mentioning the bot in that
 * chat (usually replying to an error alert) starts an insights-chat turn that
 * fetches the full log from Graylog and answers in the Telegram thread. The
 * conversation also appears in the project's chat history in the UI.
 */
export function TelegramIntegration({ envConfig, updateEnvConfig }: TelegramIntegrationProps) {
  const { t } = useTranslation('settings');

  // Local drafts for text fields — committed on blur so we don't PATCH the
  // project .env on every keystroke. Secrets (bot token, Graylog password)
  // are write-only: GET never echoes them, only *Set flags.
  const [draft, setDraft] = useState({
    telegramBotToken: '',
    telegramChatId: '',
    graylogUrl: '',
    graylogUsername: '',
    graylogPassword: ''
  });

  // Seed drafts once from the first loaded envConfig. Re-seeding on every
  // envConfig change would clobber a field the user is typing in whenever
  // another field's blur-commit PATCH round-trips.
  const seededRef = useRef(false);
  useEffect(() => {
    if (envConfig && !seededRef.current) {
      seededRef.current = true;
      setDraft(prev => ({
        ...prev,
        telegramChatId: envConfig.telegramChatId || '',
        graylogUrl: envConfig.graylogUrl || '',
        graylogUsername: envConfig.graylogUsername || ''
      }));
    }
  }, [envConfig]);

  if (!envConfig) return null;

  const commit = (key: keyof typeof draft, opts?: { secret?: boolean }) => {
    // Secrets: empty draft means "keep the stored value", never a deletion.
    if (opts?.secret) {
      if (draft[key]) updateEnvConfig({ [key]: draft[key] });
      return;
    }
    if ((envConfig[key] || '') !== draft[key]) {
      updateEnvConfig({ [key]: draft[key] });
    }
  };

  return (
    <div className="space-y-6">
      {/* Enable toggle */}
      <div className="flex items-center justify-between">
        <div className="space-y-0.5">
          <div className="flex items-center gap-2">
            <Send className="h-4 w-4 text-info" />
            <Label className="font-normal text-foreground">{t('projectSections.telegram.enable')}</Label>
          </div>
          <p className="text-xs text-muted-foreground pl-6">
            {t('projectSections.telegram.enableDescription')}
          </p>
        </div>
        <Switch
          checked={envConfig.telegramEnabled || false}
          onCheckedChange={(checked) => updateEnvConfig({ telegramEnabled: checked })}
        />
      </div>

      {envConfig.telegramEnabled && (
        <>
          {/* How it works */}
          <div className="rounded-lg border border-info/30 bg-info/5 p-3">
            <div className="flex items-start gap-3">
              <Info className="h-5 w-5 text-info mt-0.5 shrink-0" />
              <p className="text-xs text-muted-foreground">
                {t('projectSections.telegram.howItWorks')}
              </p>
            </div>
          </div>

          {/* Bot token */}
          <div className="space-y-2">
            <Label className="text-sm font-medium text-foreground">
              {t('projectSections.telegram.botToken')}
            </Label>
            <p className="text-xs text-muted-foreground">
              {t('projectSections.telegram.botTokenHint')}
            </p>
            <div onBlur={() => commit('telegramBotToken', { secret: true })}>
              <PasswordInput
                value={draft.telegramBotToken}
                onChange={(value) => setDraft(prev => ({ ...prev, telegramBotToken: value }))}
                placeholder={envConfig.telegramBotTokenSet
                  ? t('projectSections.telegram.secretConfigured')
                  : '123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'}
              />
            </div>
          </div>

          {/* Chat ID */}
          <div className="space-y-2">
            <Label className="text-sm font-medium text-foreground">
              {t('projectSections.telegram.chatId')}
            </Label>
            <p className="text-xs text-muted-foreground">
              {t('projectSections.telegram.chatIdHint')}
            </p>
            <Input
              placeholder="-1001234567890"
              value={draft.telegramChatId}
              onChange={(e) => setDraft(prev => ({ ...prev, telegramChatId: e.target.value }))}
              onBlur={() => commit('telegramChatId')}
            />
          </div>

          <Separator />

          {/* Graylog reader */}
          <div className="space-y-4">
            <div className="space-y-0.5">
              <div className="flex items-center gap-2">
                <ScrollText className="h-4 w-4 text-info" />
                <Label className="text-sm font-semibold text-foreground">
                  {t('projectSections.telegram.graylogTitle')}
                </Label>
              </div>
              <p className="text-xs text-muted-foreground pl-6">
                {t('projectSections.telegram.graylogDescription')}
              </p>
            </div>

            <div className="space-y-2 pl-6">
              <Label className="text-sm font-medium text-foreground">
                {t('projectSections.telegram.graylogUrl')}
              </Label>
              <Input
                placeholder="http://192.168.88.10:9000"
                value={draft.graylogUrl}
                onChange={(e) => setDraft(prev => ({ ...prev, graylogUrl: e.target.value }))}
                onBlur={() => commit('graylogUrl')}
              />
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pl-6">
              <div className="space-y-2">
                <Label className="text-sm font-medium text-foreground">
                  {t('projectSections.telegram.graylogUsername')}
                </Label>
                <Input
                  value={draft.graylogUsername}
                  onChange={(e) => setDraft(prev => ({ ...prev, graylogUsername: e.target.value }))}
                  onBlur={() => commit('graylogUsername')}
                />
              </div>
              <div className="space-y-2">
                <Label className="text-sm font-medium text-foreground">
                  {t('projectSections.telegram.graylogPassword')}
                </Label>
                <div onBlur={() => commit('graylogPassword', { secret: true })}>
                  <PasswordInput
                    value={draft.graylogPassword}
                    onChange={(value) => setDraft(prev => ({ ...prev, graylogPassword: value }))}
                    placeholder={envConfig.graylogPasswordSet
                      ? t('projectSections.telegram.secretConfigured')
                      : ''}
                  />
                </div>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
