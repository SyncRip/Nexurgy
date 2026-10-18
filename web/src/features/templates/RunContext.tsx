// Run detail additions (design §5.5): what the run is for (task or exploration,
// purpose), which template version it ran (or draft test run / legacy run), where
// it came from (rerun of) and what came after (reruns); then the frozen input
// versions and the registered output assets, each linking to the asset page.

import { ArrowDownToLine, ArrowUpFromLine, Compass, CornerDownRight, ListChecks, RotateCw } from 'lucide-react';
import { Link } from 'react-router-dom';
import { getAssetVersion } from '../../api/assets';
import { getWorkflow } from '../../api/endpoints';
import type { RunAssetVersion, RunInputBinding, WorkflowRun } from '../../api/types';
import { paths } from '../../app/nav';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { assetTypeLabel, AssetTypeIcon, VersionLabels } from '../tasks/common';
import type { AssetType } from '../../api/pm';
import { nodeDisplayName, stageLabel } from '../workflow/labels';
import { RunStatusChip } from '../workflow/icons';
import { RunTemplateRef } from './common';

/** Task / template version / purpose / rerun lineage, as extra cells of the run header. */
export function RunContextMeta({ run }: { run: WorkflowRun }) {
  const { t } = useI18n();
  // Draft and legacy runs carry no version brief: show the template's current name.
  const wf = useResource((signal) => getWorkflow(run.workflow_id!, { signal }), [run.workflow_id], { enabled: !!run.workflow_id && !run.template_version });
  return (
    <>
      <div>
        <dt>{t('tpl.run.context')}</dt>
        <dd>
          {run.task ? (
            <Link to={paths.task(run.task.id)} className="linkish" title={run.task.title}>
              <ListChecks aria-hidden className="inline-ico" /> <span className="mono">{run.task.key}</span> {run.task.title}
            </Link>
          ) : run.task_id ? (
            <Link to={paths.task(run.task_id)} className="linkish">
              <ListChecks aria-hidden className="inline-ico" /> {t('tpl.mon.task')}
            </Link>
          ) : run.origin === 'workspace' ? (
            <span className="faint">—</span>
          ) : (
            <span>
              <Compass aria-hidden className="inline-ico" /> {t('tpl.mon.exploration')}
            </span>
          )}
        </dd>
      </div>
      <div>
        <dt>{t('tpl.run.template')}</dt>
        <dd>
          <RunTemplateRef run={run} name={wf.data?.name} />
        </dd>
      </div>
      {run.rerun_of && (
        <div>
          <dt>{t('tpl.run.rerunOf')}</dt>
          <dd>
            <Link to={paths.run(run.rerun_of)} className="linkish">
              <CornerDownRight aria-hidden className="inline-ico" /> {run.rerun_of_run?.title || t('wf.run.untitled')}
            </Link>
          </dd>
        </div>
      )}
    </>
  );
}

export function RunPurpose({ run }: { run: WorkflowRun }) {
  const { t } = useI18n();
  if (!run.purpose && !run.reruns?.length) return null;
  return (
    <div className="tpl-run-extra">
      {run.purpose && (
        <p className="small">
          <span className="faint">{t('tpl.start.purpose')}: </span>
          <span className="tpl-pre">{run.purpose}</span>
        </p>
      )}
      {!!run.reruns?.length && (
        <p className="small tpl-reruns">
          <RotateCw aria-hidden className="inline-ico" /> <span className="faint">{t('tpl.run.reruns')}: </span>
          {run.reruns.map((r, i) => (
            <span key={r.id}>
              {i > 0 ? ' · ' : ''}
              <Link to={paths.run(r.id)} className="linkish">
                {r.title || t('wf.run.untitled')}
              </Link>{' '}
              <RunStatusChip status={r.status} />
            </span>
          ))}
        </p>
      )}
    </div>
  );
}

function VersionLine({ v, fallbackType }: { v: RunAssetVersion | null | undefined; fallbackType?: string }) {
  const { t } = useI18n();
  if (v === undefined) return <span className="faint">{t('common.loading')}</span>;
  if (!v || v.missing) return <span className="faint">{t('tpl.run.versionMissing')}</span>;
  const type = (v.asset_type ?? fallbackType) as AssetType | undefined;
  const body = (
    <>
      <span className="tk-vref-type" title={type ? assetTypeLabel(t, type) : undefined}>
        <AssetTypeIcon type={type} />
      </span>
      <span className="ellip">{v.asset_name ?? (type ? assetTypeLabel(t, type) : '')}</span> <span className="mono strong">{v.label ?? (v.version_no ? `#${v.version_no}` : '')}</span>
    </>
  );
  return (
    <span className="tpl-vline">
      {v.asset_id ? (
        <Link to={paths.asset(v.asset_id, v.id)} className="tpl-ref">
          {body}
        </Link>
      ) : (
        <span className="tpl-ref">{body}</span>
      )}
      <VersionLabels labels={v.labels} />
    </span>
  );
}

function BindingRow({ run, b }: { run: WorkflowRun; b: RunInputBinding }) {
  const { t } = useI18n();
  // Older servers only send ids: read the catalogue version for names and links.
  const fetched = useResource((signal) => getAssetVersion(b.asset_version_id, { signal }), [b.asset_version_id], { enabled: b.version === undefined });
  const v = b.version !== undefined ? b.version : fetched.error ? null : (fetched.data as unknown as RunAssetVersion | undefined);
  const node = run.graph.nodes.find((n) => n.id === b.port_key);
  const q = b.snapshot?.quality;
  const eng = b.snapshot?.engineering;
  return (
    <li>
      <span className="tpl-io-node strong">{node ? nodeDisplayName(t, node) : b.port_key}</span>
      <VersionLine v={v} fallbackType={b.snapshot?.asset_type} />
      {eng && (
        <span className={`small ${eng.approved ? '' : 'faint'}`}>
          {eng.approved ? t('tpl.run.approvedAt', { stage: stageLabel(t, eng.stage) }) : t('tpl.run.notApprovedAt', { stage: stageLabel(t, eng.stage) })}
        </span>
      )}
      {q && q.availability && q.availability !== 'ready' && <span className="small faint">{q.availability}</span>}
      <span className="mono faint small" title={b.content_hash}>
        {b.content_hash.slice(0, 10)}
      </span>
    </li>
  );
}

export function RunIoPanels({ run }: { run: WorkflowRun }) {
  const { t } = useI18n();
  const bindings = run.input_bindings ?? [];
  const outputs = run.outputs ?? [];
  if (run.origin === 'workspace' && !bindings.length && !outputs.length) return null;
  const nodeName = (id: string) => {
    const n = run.graph.nodes.find((x) => x.id === id);
    return n ? nodeDisplayName(t, n) : id;
  };
  return (
    <div className="tpl-run-panels">
      <section className="panel" aria-labelledby="tpl-run-in">
        <div className="panel-h">
          <ArrowDownToLine aria-hidden className="tpl-panel-ico" />
          <h2 id="tpl-run-in">{t('tpl.run.inputs')}</h2>
        </div>
        <div className="panel-b">
          {bindings.length ? (
            <ul className="tpl-io-list">
              {bindings.map((b) => (
                <BindingRow key={b.port_key} run={run} b={b} />
              ))}
            </ul>
          ) : (
            <p className="faint small">{t('tpl.run.noInputs')}</p>
          )}
          <p className="faint small">{t('tpl.run.inputsFrozen')}</p>
        </div>
      </section>
      <section className="panel" aria-labelledby="tpl-run-out">
        <div className="panel-h">
          <ArrowUpFromLine aria-hidden className="tpl-panel-ico" />
          <h2 id="tpl-run-out">{t('tpl.run.outputs')}</h2>
        </div>
        <div className="panel-b">
          {outputs.length ? (
            <ul className="tpl-io-list">
              {outputs.map((o) => (
                <li key={`${o.node_id}:${o.output_key}:${o.asset_version_id}`}>
                  <span className="tpl-io-node strong">{nodeName(o.node_id)}</span>
                  <VersionLine v={o.version} />
                  {o.attempt > 1 && <span className="faint small">{t('tpl.run.attempt', { n: o.attempt })}</span>}
                </li>
              ))}
            </ul>
          ) : (
            <p className="faint small">{run.outputs ? t('tpl.run.noOutputs') : t('tpl.run.outputsUnknown')}</p>
          )}
        </div>
      </section>
    </div>
  );
}
