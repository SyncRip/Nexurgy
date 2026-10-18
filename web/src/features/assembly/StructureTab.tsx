// Structure tab: instance tree ↔ 3D viewport (select, hide, isolate) and the properties of the selection
// (instance id, definition + revision, local / world transform, solve state) with the edit actions of a
// direct child of the latest revision (move, fix, suppress, replace one instance, update a shared definition,
// linear pattern, remove) and "add instance".

import { Box,ChevronDown,ChevronRight,Eye,EyeOff,Focus,Layers,Plus } from 'lucide-react';
import { useEffect,useMemo,useRef,useState,type KeyboardEvent } from 'react';
import { Link } from 'react-router-dom';
import type { AssemblyRevision,Component,Scene,SceneNode,SolveInstanceInfo } from '../../api/assemblies';
import { assemblyRevisionPath } from '../../app/versionLinks';
import { Chip } from '../../components/ui/Badges';
import { Dialog } from '../../components/ui/Dialog';
import { Field } from '../../components/ui/Form';
import { useI18n } from '../../i18n/I18nProvider';
import { AssemblyViewport } from './AssemblyViewport';
import { DefinitionPicker,definitionInput,type DefinitionChoice } from './DefinitionPicker';
import { useAssemblyDraft, type EditApi } from './edit';
import { decompose,findNode,flattenTree,fmtNum,pathKey } from './model';

export function StructureTab({
  rev,
  scene,
  projectId,
  canEdit,
  edit,
  onSelection,
}: {
  rev: AssemblyRevision;
  scene: Scene | undefined;
  projectId: string;
  canEdit: boolean;
  edit: EditApi;
  onSelection?: (path: string[] | null) => void;
}) {
  const { t } = useI18n();
  // The saved root is a presentation row, never a fabricated instance or mesh path.
  const [selection, setSelection] = useState<{ kind: 'root' } | { kind: 'instance'; path: string } | null>(null);
  const selected = selection?.kind === 'instance' ? selection.path : null;
  const setSelected = (path: string | null) => setSelection(path ? { kind: 'instance', path } : null);
  const [rootCollapsed, setRootCollapsed] = useState(false);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [isolated, setIsolated] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const rows = useMemo(() => flattenTree(scene?.tree ?? [], collapsed), [scene, collapsed]);
  const treeRef = useRef<HTMLUListElement>(null);
  // A pick in the viewport reveals the row: expand its ancestors and scroll it into view.
  useEffect(() => {
    if (!selected) return;
    setRootCollapsed(false);
    const parts = selected.split('/');
    const ancestors = parts.slice(0, -1).map((_, i) => parts.slice(0, i + 1).join('/'));
    setCollapsed((c) => (ancestors.some((a) => c.has(a)) ? new Set([...c].filter((k) => !ancestors.includes(k))) : c));
    requestAnimationFrame(() => treeRef.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' }));
  }, [selected]);
  const node = selected && scene ? findNode(scene.tree, selected) : undefined;
  useEffect(() => {
    setSelection(null);
    setRootCollapsed(false);
    setCollapsed(new Set());
    setHidden(new Set());
    setIsolated(null);
  }, [rev.id]);
  const selectedPath = node?.path.join('/') ?? null;
  useEffect(() => onSelection?.(selectedPath ? selectedPath.split('/') : null), [selectedPath, onSelection]);
  const direct = node && node.path.length === 1 ? rev.components.find((c) => c.instance_id === node.instance_id) : undefined;
  const solve = node && node.path.length === 1 ? rev.solve.instances?.[node.instance_id] : undefined;

  const toggle = (set: Set<string>, key: string) => {
    const next = new Set(set);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    return next;
  };
  const selectRoot = () => setSelection({ kind: 'root' });
  const rootName = rev.assembly?.name || t('asm.tree.kind.root');
  const treeKey = (event: KeyboardEvent<HTMLLIElement>, key: string | null, hasChildren: boolean, isCollapsed: boolean) => {
    const target = event.currentTarget;
    const items = [...(treeRef.current?.querySelectorAll<HTMLLIElement>('[role="treeitem"]') ?? [])];
    const index = items.indexOf(target);
    const focus = (item: HTMLLIElement | undefined) => {
      if (!item) return;
      item.focus();
      if (item.dataset.instancePath) setSelected(item.dataset.instancePath);
      else selectRoot();
    };
    const expand = (value: boolean) => key === null ? setRootCollapsed(!value) : setCollapsed((current) => {
      const next = new Set(current);
      if (value) next.delete(key); else next.add(key);
      return next;
    });
    switch (event.key) {
      case 'ArrowDown': focus(items[index + 1]); break;
      case 'ArrowUp': focus(items[index - 1]); break;
      case 'Home': focus(items[0]); break;
      case 'End': focus(items[items.length - 1]); break;
      case 'ArrowRight': if (hasChildren && isCollapsed) expand(true); else if (hasChildren) focus(items[index + 1]); break;
      case 'ArrowLeft':
        if (hasChildren && !isCollapsed) expand(false);
        else if (key !== null) {
          const parent = key.split('/').slice(0, -1).join('/');
          focus(parent ? items.find((item) => item.dataset.instancePath === parent) : items[0]);
        }
        break;
      case 'Enter':
      case ' ':
        if (event.target !== target) return;
        if (key === null) selectRoot(); else setSelected(key);
        break;
      default: return;
    }
    event.preventDefault();
    event.stopPropagation();
  };

  return (
    <div className="asm-structure">
      <section className="panel asm-tree" aria-label={t('asm.tree.label')}>
        <div className="panel-h row">
          <strong>{t('asm.tree.label')}</strong>
          <span className="grow" />
          {isolated && (
            <button type="button" className="btn sm" onClick={() => setIsolated(null)}>
              {t('asm.tree.unisolate')}
            </button>
          )}
          {canEdit && (
            <button type="button" className="btn sm" onClick={() => setAdding(true)}>
              <Plus aria-hidden /> {t('asm.tree.addInstance')}
            </button>
          )}
        </div>
        <ul className="asm-tree-list" role="tree" ref={treeRef} aria-label={t('asm.tree.label')}>
          <li role="treeitem" aria-level={1} aria-posinset={1} aria-setsize={1} aria-selected={selection?.kind === 'root'} aria-expanded={rows.length ? !rootCollapsed : undefined}
            tabIndex={selection?.kind !== 'instance' ? 0 : -1} className={`asm-tree-row asm-tree-root ${selection?.kind === 'root' ? 'sel' : ''}`}
            onKeyDown={(event) => treeKey(event, null, rows.length > 0, rootCollapsed)}>
            {rows.length > 0 ? <button type="button" className="btn ghost sm icon" onClick={() => setRootCollapsed((value) => !value)} aria-label={t(rootCollapsed ? 'asm.tree.expand' : 'asm.tree.collapse')}>{rootCollapsed ? <ChevronRight /> : <ChevronDown />}</button> : <span className="asm-tree-spacer" />}
            <button type="button" className="asm-tree-name linkish" onClick={selectRoot} title={`${rev.assembly?.number ?? ''} ${rootName} · ${rev.label}`}>
              <Layers aria-hidden className="inline-ico" />
              <span className="asm-tree-name-copy"><span className="ellip">{rootName}</span><span className="asm-tree-meta"><span>{t('asm.tree.kind.root')}</span><span className="mono">{rev.label}</span></span></span>
            </button>
          </li>
          {!rootCollapsed && rows.map(({ node: n, key, depth, hasChildren, position, siblings }) => {
            const isHidden = hidden.has(key);
            return (
              <li
                key={key}
                role="treeitem"
                aria-level={depth + 2}
                aria-posinset={position}
                aria-setsize={siblings}
                aria-selected={selected === key}
                aria-expanded={hasChildren ? !collapsed.has(key) : undefined}
                tabIndex={selected === key ? 0 : -1}
                data-instance-path={key}
                onKeyDown={(event) => treeKey(event, key, hasChildren, collapsed.has(key))}
                className={`asm-tree-row ${selected === key ? 'sel' : ''} ${n.suppressed ? 'supp' : ''}`}
                style={{ paddingLeft: 6 + (depth + 1) * 14 }}
              >
                {hasChildren ? (
                  <button type="button" className="btn ghost sm icon" onClick={() => setCollapsed((s) => toggle(s, key))} aria-label={collapsed.has(key) ? t('asm.tree.expand') : t('asm.tree.collapse')}>
                    {collapsed.has(key) ? <ChevronRight /> : <ChevronDown />}
                  </button>
                ) : (
                  <span className="asm-tree-spacer" />
                )}
                <button
                  type="button"
                  className="asm-tree-name linkish"
                  onClick={() => setSelected(key)}
                  title={`${n.instance_id} · ${n.definition.number ?? ''} ${n.definition.name ?? ''} ${n.definition.label ?? ''}`}
                >
                  {n.definition.kind === 'assembly' ? <Layers aria-hidden className="inline-ico" /> : <Box aria-hidden className="inline-ico" />}
                  <span className="asm-tree-name-copy"><span className="ellip">{n.name}</span><span className="asm-tree-meta"><span>{t(n.definition.kind === 'assembly' ? 'asm.tree.kind.assembly' : 'asm.tree.kind.part')}</span><span className="mono">{n.definition.label ?? ''}</span></span></span>
                </button>
                <span className="asm-tree-flags">
                  {n.suppressed && <span className="tag">{t('asm.tree.suppressed')}</span>}
                  {n.fixed && depth === 0 && <span className="tag">{t('asm.tree.fixed')}</span>}
                  {n.update_available && <span className="tag warn">{t('asm.tree.update')}</span>}
                </span>
                <button type="button" className="btn ghost sm icon" onClick={() => setHidden((s) => toggle(s, key))} aria-label={isHidden ? t('asm.tree.show') : t('asm.tree.hide')} title={isHidden ? t('asm.tree.show') : t('asm.tree.hide')}>
                  {isHidden ? <EyeOff /> : <Eye />}
                </button>
                <button type="button" className={`btn ghost sm icon ${isolated === key ? 'active' : ''}`} onClick={() => setIsolated(isolated === key ? null : key)} aria-label={t('asm.tree.isolate')} title={t('asm.tree.isolate')}>
                  <Focus />
                </button>
              </li>
            );
          })}
        </ul>
      </section>
      <section className="asm-view">
        <AssemblyViewport scene={scene} selected={selected} hidden={hidden} isolated={isolated} onPick={setSelected} />
      </section>
      <section className="panel asm-props" aria-label={t('asm.props.title')}>
        <div className="panel-h">
          <strong>{t('asm.props.title')}</strong>
        </div>
        <div className="panel-b">
          {selection?.kind === 'root' ? (
            <dl className="asm-dl">
              <dt>{t('asm.props.name')}</dt><dd>{rootName}</dd>
              <dt>{t('asm.props.definition')}</dt><dd>{t('asm.tree.kind.root')} · {rev.assembly?.number}</dd>
              <dt>{t('asm.props.revision')}</dt><dd className="mono">{rev.label}</dd>
              <dt>{t('asm.tree.savedChildren')}</dt><dd>{rev.components.length}</dd>
            </dl>
          ) : !node ? <p className="faint small">{t('asm.props.none')}</p> : <Properties node={node} direct={direct} solve={solve} rev={rev} canEdit={canEdit} edit={edit} projectId={projectId} />}
        </div>
      </section>
      {adding && <AddInstanceDialog projectId={projectId} rev={rev} edit={edit} onClose={() => setAdding(false)} />}
    </div>
  );
}

function Vec({ v }: { v: number[] }) {
  return <span className="mono">({v.map((x) => fmtNum(x)).join(', ')})</span>;
}

function Properties({
  node,
  direct,
  solve,
  rev,
  canEdit,
  edit,
  projectId,
}: {
  node: SceneNode;
  direct: Component | undefined;
  solve: SolveInstanceInfo | undefined;
  rev: AssemblyRevision;
  canEdit: boolean;
  edit: EditApi;
  projectId: string;
}) {
  const { t } = useI18n();
  const [dialog, setDialog] = useState<'move' | 'replace' | 'pattern' | null>(null);
  const local = decompose(node.transform);
  const world = decompose(node.world);
  const d = node.definition;
  const iid = node.instance_id;
  return (
    <div className="stack-sm">
      <dl className="asm-dl">
        <dt>{t('asm.props.instance')}</dt>
        <dd className="mono">{node.path.join(' / ')}</dd>
        <dt>{t('asm.props.name')}</dt>
        <dd>{node.name}</dd>
        <dt>{t('asm.props.definition')}</dt>
        <dd>
          {t(d.kind === 'part' ? 'asm.props.kind.part' : 'asm.props.kind.assembly')} · <span className="mono">{d.number}</span> {d.name}
        </dd>
        <dt>{t('asm.props.revision')}</dt>
        <dd className="mono">
          {d.label}
          {node.update_available && (
            <Chip tone="warn" className="asm-chip-gap">
              → R/A{node.update_available.latest_rev_no}
            </Chip>
          )}
        </dd>
        {d.kind === 'part' && (
          <>
            <dt>{t('asm.props.material')}</dt>
            <dd>{d.material ?? <span className="faint">{t('asm.props.unknown')}</span>}</dd>
          </>
        )}
        <dt>{t('asm.props.local')}</dt>
        <dd>
          <div className="small">
            {t('asm.props.translate')} <Vec v={local.translate} />
          </div>
          <div className="small">
            {t('asm.props.rotate')} <Vec v={local.rotate} />
          </div>
        </dd>
        <dt>{t('asm.props.world')}</dt>
        <dd>
          <div className="small">
            {t('asm.props.translate')} <Vec v={world.translate} />
          </div>
          <div className="small">
            {t('asm.props.rotate')} <Vec v={world.rotate} />
          </div>
        </dd>
        <dt>{t('asm.props.placement')}</dt>
        <dd className="small">
          {node.fixed ? t('asm.tree.fixed') : ''} {node.placement?.mode ?? ''} {node.placement?.source ? `· ${node.placement.source}` : ''}
        </dd>
        {solve && (
          <>
            <dt>{t('asm.props.dof')}</dt>
            <dd className="small">
              {t(`asm.solve.${solve.status}`)}
              {solve.dof !== null && solve.dof > 0 ? ` · ${t('asm.props.dofValue', { n: solve.dof })}` : ''}
              {solve.over_constrained ? ` · ${t('asm.solve.over')}` : ''}
              {solve.free.length > 0 && (
                <ul className="asm-free">
                  {solve.free.map((f, i) => (
                    <li key={i}>
                      {t(`asm.props.free.${f.motion}`)} <Vec v={f.axis} />
                    </li>
                  ))}
                </ul>
              )}
            </dd>
          </>
        )}
      </dl>
      {node.path.length > 1 && (
        <p className="callout slim info">
          {t('asm.props.nested')}{' '}
        </p>
      )}
      {d.kind === 'assembly' && d.assembly_id && (
        <Link className="btn sm" to={assemblyRevisionPath(d.assembly_id, d.id)}>
          {t('asm.props.openSub')}
        </Link>
      )}
      {canEdit && direct && (
        <div className="row asm-actions">
          <button type="button" className="btn sm" onClick={() => setDialog('move')} disabled={edit.busy}>
            {t('asm.edit.move')}
          </button>
          <button type="button" className="btn sm" disabled={edit.busy} onClick={() => void edit.submit([{ op: 'set_fixed', instance: iid, fixed: !direct.fixed }])}>
            {direct.fixed ? t('asm.edit.unfix') : t('asm.edit.fix')}
          </button>
          <button type="button" className="btn sm" disabled={edit.busy} onClick={() => void edit.submit([{ op: 'suppress_instance', instance: iid, suppressed: !direct.suppressed }])}>
            {direct.suppressed ? t('asm.edit.unsuppress') : t('asm.edit.suppress')}
          </button>
          <button type="button" className="btn sm" onClick={() => setDialog('replace')} disabled={edit.busy}>
            {t('asm.edit.replace')}
          </button>
          {direct.update_available && (
            <button
              type="button"
              className="btn sm primary"
              disabled={edit.busy}
              onClick={() =>
                void edit.submit([
                  d.kind === 'part'
                    ? { op: 'update_definition', part_id: d.part_id, revision_id: direct.update_available!.latest_revision_id }
                    : { op: 'update_definition', assembly_id: d.assembly_id, revision_id: direct.update_available!.latest_revision_id },
                ])
              }
            >
              {t('asm.props.updateTo', { label: `${d.kind === 'part' ? 'R' : 'A'}${direct.update_available.latest_rev_no}` })}
            </button>
          )}
          <button type="button" className="btn sm" onClick={() => setDialog('pattern')} disabled={edit.busy}>
            {t('asm.edit.pattern')}
          </button>
          <button type="button" className="btn sm danger" disabled={edit.busy} onClick={() => void edit.submit([{ op: 'remove_instance', instance: iid }])}>
            {t('asm.edit.remove')}
          </button>
        </div>
      )}
      {dialog === 'move' && direct && <MoveDialog comp={direct} edit={edit} onClose={() => setDialog(null)} />}
      {dialog === 'replace' && direct && <ReplaceDialog comp={direct} edit={edit} projectId={projectId} rev={rev} onClose={() => setDialog(null)} />}
      {dialog === 'pattern' && direct && <PatternDialog comp={direct} edit={edit} onClose={() => setDialog(null)} />}
    </div>
  );
}

function NumTriple({ value, onChange, label }: { value: string[]; onChange: (v: string[]) => void; label: string }) {
  return (
    <Field label={label}>
      <div className="asm-triple">
        {value.map((v, i) => (
          <input
            key={i}
            className="input mono"
            inputMode="decimal"
            value={v}
            aria-label={`${label} ${'XYZ'[i]}`}
            onChange={(e) => onChange(value.map((x, j) => (j === i ? e.target.value : x)))}
          />
        ))}
      </div>
    </Field>
  );
}

const nums = (v: string[]) => v.map((x) => Number(x));
const valid = (v: string[]) => v.every((x) => x.trim() !== '' && Number.isFinite(Number(x)));

function MoveDialog({ comp, edit, onClose }: { comp: Component; edit: EditApi; onClose: () => void }) {
  useAssemblyDraft(edit);
  const { t } = useI18n();
  const cur = decompose(comp.transform);
  const [tr, setTr] = useState(cur.translate.map(String));
  const [rot, setRot] = useState(cur.rotate.map(String));
  const ok = valid(tr) && valid(rot);
  return (
    <Dialog
      title={t('asm.edit.moveTitle', { name: comp.name })}
      onClose={onClose}
      busy={edit.busy}
      onSubmit={async () => {
        if (ok && (await edit.submit([{ op: 'move_instance', instance: comp.instance_id, placement: { translate: nums(tr), rotate: nums(rot) } }]))) onClose();
      }}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={!ok || edit.busy}>
            {t('asm.edit.apply')}
          </button>
        </>
      }
    >
      <NumTriple label={t('asm.props.translate')} value={tr} onChange={setTr} />
      <NumTriple label={t('asm.props.rotate')} value={rot} onChange={setRot} />
    </Dialog>
  );
}

function ReplaceDialog({ comp, edit, projectId, rev, onClose }: { comp: Component; edit: EditApi; projectId: string; rev: AssemblyRevision; onClose: () => void }) {
  useAssemblyDraft(edit);
  const { t } = useI18n();
  const [choice, setChoice] = useState<DefinitionChoice>({ kind: comp.definition.kind, itemId: '', revisionId: '' });
  return (
    <Dialog
      title={t('asm.edit.replaceTitle', { name: comp.name })}
      onClose={onClose}
      onSubmit={async () => {
        if (!choice.itemId) return;
        onClose();
        await edit.submit([{ op: 'replace_definition', instance: comp.instance_id, definition: definitionInput(choice) }]);
      }}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={!choice.itemId}>
            {t('asm.edit.apply')}
          </button>
        </>
      }
    >
      <DefinitionPicker projectId={projectId} value={choice} onChange={setChoice} excludeAssemblyId={rev.assembly_id} />
    </Dialog>
  );
}

function PatternDialog({ comp, edit, onClose }: { comp: Component; edit: EditApi; onClose: () => void }) {
  useAssemblyDraft(edit);
  const { t } = useI18n();
  const [count, setCount] = useState('3');
  const [step, setStep] = useState(['20', '0', '0']);
  const ok = valid(step) && Number.isInteger(Number(count)) && Number(count) >= 2;
  return (
    <Dialog
      title={t('asm.edit.patternTitle', { name: comp.name })}
      onClose={onClose}
      onSubmit={async () => {
        if (!ok) return;
        onClose();
        await edit.submit([{ op: 'pattern_linear', instance: comp.instance_id, count: Number(count), step: nums(step) }]);
      }}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={!ok}>
            {t('asm.edit.apply')}
          </button>
        </>
      }
    >
      <Field label={t('asm.edit.count')}>
        <input className="input mono" inputMode="numeric" value={count} onChange={(e) => setCount(e.target.value)} />
      </Field>
      <NumTriple label={t('asm.edit.step')} value={step} onChange={setStep} />
    </Dialog>
  );
}

function AddInstanceDialog({ projectId, rev, edit, onClose }: { projectId: string; rev: AssemblyRevision; edit: EditApi; onClose: () => void }) {
  useAssemblyDraft(edit);
  const { t } = useI18n();
  const [choice, setChoice] = useState<DefinitionChoice>({ kind: 'part', itemId: '', revisionId: '' });
  const [name, setName] = useState('');
  const [tr, setTr] = useState(['0', '0', '0']);
  const [rot, setRot] = useState(['0', '0', '0']);
  const [fixed, setFixed] = useState(false);
  const ok = !!choice.itemId && valid(tr) && valid(rot);
  return (
    <Dialog
      title={t('asm.add.title')}
      onClose={onClose}
      onSubmit={async () => {
        if (!ok) return;
        onClose();
        await edit.submit([
          { op: 'add_instance', name: name || undefined, definition: definitionInput(choice), placement: { translate: nums(tr), rotate: nums(rot) }, fixed },
        ]);
      }}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={!ok}>
            {t('asm.edit.apply')}
          </button>
        </>
      }
    >
      <DefinitionPicker projectId={projectId} value={choice} onChange={setChoice} excludeAssemblyId={rev.assembly_id} />
      <Field label={t('asm.add.name')} optional>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
      </Field>
      <NumTriple label={t('asm.props.translate')} value={tr} onChange={setTr} />
      <NumTriple label={t('asm.props.rotate')} value={rot} onChange={setRot} />
      <label className="check">
        <input type="checkbox" checked={fixed} onChange={(e) => setFixed(e.target.checked)} /> {t('asm.add.fixed')}
      </label>
    </Dialog>
  );
}

export function sceneKey(n: SceneNode) {
  return pathKey(n.path);
}
