// Picks an exact definition for an instance: a part (latest built revision or a chosen one) or a sub-assembly
// (latest or a chosen revision). The server resolves and checks project, build state and cycles.

import { useMemo } from 'react';
import { listAssemblies, listAssemblyRevisions, type DefinitionKind } from '../../api/assemblies';
import { listParts, listRevisions } from '../../api/endpoints';
import { Field } from '../../components/ui/Form';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';

export interface DefinitionChoice {
  kind: DefinitionKind;
  itemId: string; // part id or assembly id
  revisionId: string; // '' = latest
}

export function DefinitionPicker({
  projectId,
  value,
  onChange,
  excludeAssemblyId,
  allowAssembly = true,
}: {
  projectId: string;
  value: DefinitionChoice;
  onChange: (v: DefinitionChoice) => void;
  excludeAssemblyId?: string;
  allowAssembly?: boolean;
}) {
  const { t } = useI18n();
  const parts = useResource((signal) => listParts(projectId, {}, { signal }), [projectId], { enabled: value.kind === 'part' });
  const asms = useResource((signal) => listAssemblies(projectId, {}, { signal }), [projectId], { enabled: value.kind === 'assembly' });
  const partRevs = useResource((signal) => listRevisions(value.itemId, { signal }), [value.itemId], { enabled: value.kind === 'part' && !!value.itemId });
  const asmRevs = useResource((signal) => listAssemblyRevisions(value.itemId, { signal }), [value.itemId], {
    enabled: value.kind === 'assembly' && !!value.itemId,
  });
  const items = useMemo(() => {
    if (value.kind === 'part') return (parts.data ?? []).filter((p) => p.latest_revision).map((p) => ({ id: p.id, label: `${p.number} · ${p.name}` }));
    return (asms.data ?? []).filter((a) => a.id !== excludeAssemblyId && a.latest_revision).map((a) => ({ id: a.id, label: `${a.number} · ${a.name}` }));
  }, [value.kind, parts.data, asms.data, excludeAssemblyId]);
  const revs =
    value.kind === 'part'
      ? (partRevs.data ?? []).filter((r) => r.status === 'ready' && !r.mesh_only).map((r) => ({ id: r.id, label: r.label }))
      : (asmRevs.data ?? []).map((r) => ({ id: r.id, label: r.label }));
  const latestLabel = revs[0]?.label ?? '—';

  return (
    <>
      {allowAssembly && (
        <Field label={t('asm.add.kind')}>
          <div className="seg" role="group">
            {(['part', 'assembly'] as DefinitionKind[]).map((k) => (
              <button key={k} type="button" className="seg-btn" aria-pressed={value.kind === k} onClick={() => onChange({ kind: k, itemId: '', revisionId: '' })}>
                {t(k === 'part' ? 'asm.add.part' : 'asm.add.assembly')}
              </button>
            ))}
          </div>
        </Field>
      )}
      <Field label={t('asm.add.choose')}>
        <select className="select" value={value.itemId} onChange={(e) => onChange({ ...value, itemId: e.target.value, revisionId: '' })} required>
          <option value="">—</option>
          {items.map((i) => (
            <option key={i.id} value={i.id}>
              {i.label}
            </option>
          ))}
        </select>
      </Field>
      {value.itemId && (
        <Field label={t('asm.add.revision')} hint={revs.length ? undefined : t('asm.add.noRevision')}>
          <select className="select" value={value.revisionId} onChange={(e) => onChange({ ...value, revisionId: e.target.value })}>
            <option value="">{t('asm.add.latest', { label: latestLabel })}</option>
            {revs.map((r) => (
              <option key={r.id} value={r.id}>
                {r.label}
              </option>
            ))}
          </select>
        </Field>
      )}
    </>
  );
}

export function definitionInput(c: DefinitionChoice): { kind: DefinitionKind; revision_id?: string; part_id?: string; assembly_id?: string } {
  if (c.revisionId) return { kind: c.kind, revision_id: c.revisionId };
  return c.kind === 'part' ? { kind: 'part', part_id: c.itemId } : { kind: 'assembly', assembly_id: c.itemId };
}
