import { useFeature } from '../../../api/capabilities';
// Node palette grouped by category: drag onto the canvas or click to add.

import { Plus, Search, Sparkles } from 'lucide-react';
import { useMemo, useState } from 'react';
import type { WorkflowNodeType } from '../../../api/types';
import { useI18n } from '../../../i18n/I18nProvider';
import { NodeTypeIcon } from '../icons';
import { categoryLabel, nodeTypeDoc, nodeTypeLabel } from '../labels';
import { CATEGORIES } from '../model';

export const DND_MIME = 'application/x-partroute-node-type';

export function Palette({ defs, disabled, onAdd }: { defs: WorkflowNodeType[]; disabled: boolean; onAdd: (type: string) => void }) {
  const { t } = useI18n();
  const collaborationEnabled = useFeature('collaboration');
  const [q, setQ] = useState('');
  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const match = (d: WorkflowNodeType) =>
      !needle || d.type.includes(needle) || nodeTypeLabel(t, d.type).toLowerCase().includes(needle) || nodeTypeDoc(t, d.type, d.doc).toLowerCase().includes(needle);
    const cats = [...CATEGORIES, ...new Set(defs.map((d) => d.category).filter((c) => !(CATEGORIES as readonly string[]).includes(c)))];
    return cats.map((c) => ({ category: c, items: defs.filter((d) => d.available !== false && (d.type !== 'business_task' || collaborationEnabled) && d.category === c && match(d)) })).filter((g) => g.items.length);
  }, [defs, q, t, collaborationEnabled]);

  return (
    <aside className="wf-palette" aria-label={t('wf.palette.title')}>
      <div className="wf-palette-head">
        <h2>{t('wf.palette.title')}</h2>
        <div className="search wf-palette-search">
          <Search aria-hidden />
          <input className="input sm" type="search" value={q} placeholder={t('wf.palette.search')} aria-label={t('wf.palette.search')} onChange={(e) => setQ(e.target.value)} />
        </div>
      </div>
      <div className="wf-palette-body">
        {groups.map((g) => (
          <section key={g.category} className="wf-palette-group">
            <h3 className={`cat-${g.category}`}>{categoryLabel(t, g.category)}</h3>
            <ul>
              {g.items.map((d) => (
                <li key={d.type}>
                  <button
                    type="button"
                    className={`wf-palette-item cat-${d.category}`}
                    draggable={!disabled}
                    disabled={disabled}
                    title={nodeTypeDoc(t, d.type, d.doc)}
                    onDragStart={(e) => {
                      e.dataTransfer.setData(DND_MIME, d.type);
                      e.dataTransfer.effectAllowed = 'copy';
                    }}
                    onClick={() => onAdd(d.type)}
                  >
                    <span className="wf-palette-ico">
                      <NodeTypeIcon type={d.type} />
                    </span>
                    <span className="wf-palette-text">
                      <span className="wf-palette-name">
                        {nodeTypeLabel(t, d.type)}
                        {d.uses_ai && (
                          <span className="wf-ai-badge sm" aria-label={t('wf.ai.badgeTitle')}>
                            <Sparkles aria-hidden /> AI
                          </span>
                        )}
                      </span>
                      <span className="wf-palette-ports">
                        {d.inputs.map((p) => (
                          <span key={`i-${p.name}`} className={`wf-port-dot pt-${p.type}`} aria-hidden />
                        ))}
                        {d.inputs.length > 0 && <span className="faint">→</span>}
                        {d.outputs.map((p) => (
                          <span key={`o-${p.name}`} className={`wf-port-dot pt-${p.type}`} aria-hidden />
                        ))}
                      </span>
                    </span>
                    <Plus className="wf-palette-add" aria-hidden />
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ))}
        {!groups.length && <p className="faint small wf-pad">{t('common.noMatches')}</p>}
      </div>
      <p className="wf-palette-foot faint small">{t('wf.palette.hint')}</p>
    </aside>
  );
}
