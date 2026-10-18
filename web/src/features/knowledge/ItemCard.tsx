// One knowledge item: an exact asset version with its use kind in the archived
// run, this task's acceptance, reuse eligibility and index state. Index failure
// is shown but never hides the item or its original file.

import { ExternalLink, TriangleAlert } from 'lucide-react';
import { Link } from 'react-router-dom';
import type { KnowledgeItem } from '../../api/knowledge';
import { effectiveAcceptance } from '../../api/resources';
import { paths } from '../../app/nav';
import { Chip } from '../../components/ui/Badges';
import { useI18n } from '../../i18n/I18nProvider';
import { AcceptanceChip, ItemIndexChip, ItemKindChip, ReusableChip } from '../assets/collabMeta';
import { AssetTypeIcon, HashText, VersionToken, assetTypeLabel } from '../assets/meta';

export function ItemCard({ item, selected, onSelect, returnTo }: { item: KnowledgeItem; selected?: boolean; onSelect?: (item: KnowledgeItem) => void; returnTo: string }) {
  const { t } = useI18n();
  const v = item.version;
  const acc = effectiveAcceptance(item.acceptance, item.kind === 'history' ? 'superseded' : item.kind);
  const versionPage = paths.assetVersion(v.asset_id, item.asset_version_id, returnTo);
  const selectable = !!onSelect;
  return (
    <article className={`kb-item ${selected ? 'is-selected' : ''} ${selectable ? 'is-selectable' : ''}`} aria-current={selected ? 'true' : undefined}>
      <div className="kb-item-head">
        <span className="kb-item-ico" aria-hidden>
          <AssetTypeIcon type={v.asset_type} />
        </span>
        <div className="kb-item-titles">
          <div className="kb-item-title">
            {selectable ? (
              <button type="button" className="kb-item-pick" aria-pressed={!!selected} onClick={() => onSelect?.(item)} title={t('kb.item.pick')}>
                <span className="ellip strong" title={v.asset_name}>
                  {v.asset_name}
                </span>
                <VersionToken label={v.label} />
              </button>
            ) : (
              <>
                <span className="ellip strong" title={v.asset_name}>
                  {v.asset_name}
                </span>
                <VersionToken label={v.label} />
              </>
            )}
          </div>
          <div className="row small faint kb-item-meta">
            <span>{t('kb.item.type', { type: assetTypeLabel(t, v.asset_type) })}</span>
            <span className="row">
              {t('kb.item.hash')} <HashText hash={item.content_hash} chars={12} copy={false} />
            </span>
            {item.slot_key && <span className="mono">{item.slot_key}</span>}
            {item.task_id && (
              <Link to={paths.task(item.task_id)} className="as-use-link">
                {item.task_key || t('kb.item.task')}
              </Link>
            )}
          </div>
        </div>
        <div className="kb-item-chips">
          <ItemKindChip kind={item.kind} />
          {acc === 'approved' ? <Chip tone="ok">{t('kb.item.accepted')}</Chip> : <AcceptanceChip acceptance={item.acceptance} kind={item.kind} />}
          <ReusableChip reusable={item.reusable} />
          {item.index_status !== 'ready' && <ItemIndexChip status={item.index_status} />}
        </div>
      </div>
      {item.index_status === 'failed' && (
        <div className="callout warn slim" role="note">
          <TriangleAlert aria-hidden />
          <span>
            {t('kb.item.indexFailed')}
            {item.index_error && <span className="faint"> · {item.index_error}</span>}
          </span>
        </div>
      )}
      <div className="row kb-item-actions">
        <Link to={versionPage} className="btn sm">
          <ExternalLink /> {t('kb.item.openVersion')}
        </Link>
      </div>
    </article>
  );
}
