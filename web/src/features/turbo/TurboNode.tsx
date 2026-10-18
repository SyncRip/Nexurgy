// Turbo state of one collaboration node (W7): "done · not verified" for a node whose outputs were released
// without review or acceptance, and how a person's node was automated (bound to an agent) or why it could not be.

import './turbo.css';
import { Bot, Hand } from 'lucide-react';
import type { NodeTaskOut } from '../../api/collaboration';
import { useI18n } from '../../i18n/I18nProvider';
import { VerificationChips } from './Turbo';

export function TurboNodeNote({ node, compact }: { node: Pick<NodeTaskOut, 'verification' | 'turbo_binding' | 'phase'>; compact?: boolean }) {
  const { t } = useI18n();
  const b = node.turbo_binding;
  if (!node.verification && !b) return null;
  return (
    <span className="stack-sm">
      {node.verification && <VerificationChips verification={node.verification} />}
      {b && !compact && (
        <span className={`cl-node-note ${b.status === 'bound' ? 'warn' : 'err'}`}>
          {b.status === 'bound' ? <Bot aria-hidden /> : <Hand aria-hidden />}
          <span>{t(b.status === 'bound' ? 'tb.node.bound' : 'tb.node.notAutomatable', { reason: b.reason })}</span>
        </span>
      )}
      {b && compact && b.status === 'not_automatable' && node.phase !== 'completed_unverified' && (
        <span className="cl-node-note err">
          <Hand aria-hidden />
          <span>{t('tb.node.notAutomatable', { reason: b.reason })}</span>
        </span>
      )}
    </span>
  );
}
