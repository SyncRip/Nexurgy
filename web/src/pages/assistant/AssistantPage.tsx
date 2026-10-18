// Full AI workspace: conversation list | conversation | context inspector
// (.trellis/tasks/archive/2026-10/10-08-ai-workspace/design.md §7.3). The URL names the
// conversation (/assistant/:conversationId); the provider holds it, so going
// back to the dock shows the same conversation.

import { useEffect, useRef } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { paths } from '../../app/nav';
import { useCrumbs } from '../../components/shell/ShellContext';
import { useAssistant } from '../../features/assistant/AssistantProvider';
import { AssistantPanel } from '../../features/assistant/AssistantPanel';
import { useI18n } from '../../i18n/I18nProvider';

export default function AssistantPage() {
  const { t } = useI18n();
  const { conversationId: param } = useParams();
  const navigate = useNavigate();
  const { conversationId, selectConversation } = useAssistant();
  useCrumbs([{ label: t('as.title') }]);

  // URL → provider (when the URL changes: link, back / forward, switching in the list).
  const lastParam = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (param === lastParam.current) return;
    lastParam.current = param;
    if (param && param !== conversationId) selectConversation(param);
  }, [param, conversationId, selectConversation]);

  // Provider → URL (a conversation chosen elsewhere: fork, addContext, start page send).
  const lastId = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const changed = lastId.current !== undefined && lastId.current !== conversationId;
    const first = lastId.current === undefined;
    lastId.current = conversationId;
    if (conversationId && conversationId !== param && (changed || (first && !param))) navigate(paths.assistant(conversationId), { replace: true });
    else if (!conversationId && changed && param) navigate(paths.assistant(), { replace: true });
  }, [conversationId, param, navigate]);

  return (
    <div className="as-page">
      <AssistantPanel variant="full" />
    </div>
  );
}
