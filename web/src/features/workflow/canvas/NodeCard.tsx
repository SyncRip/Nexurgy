// Custom React Flow nodes: the workflow node card (typed, colour-coded ports,
// params summary, AI badge, validation marker, run status) and group frames.

import { Handle, Position, useStore, type NodeProps } from '@xyflow/react';
import { ChevronRight, Sparkles, TriangleAlert } from 'lucide-react';
import { memo } from 'react';
import type { NodePort } from '../../../api/types';
import { useI18n } from '../../../i18n/I18nProvider';
import type { FrameNode, WfNode } from '../flow';
import { NodeStatusChip, NodeTypeIcon } from '../icons';
import { graphErrorText, groupDisplayLabel, nodeDisplayName, portLabel, portTypeLabel } from '../labels';
import { useRevision } from '../lookups';
import { groupColor } from '../model';
import { paramLines } from '../paramFormat';
import { nodeSummary } from '../run/summary';
import { useCanvas } from './CanvasContext';
import { compactLine, NodeFooter, ResultBlock, StateBlock } from './NodeResult';

/** Below this zoom the cards collapse to title, status and a one-line result (prototype LOD_Z). */
export const LOD_ZOOM = 0.72;
const lodSelector = (s: { transform: [number, number, number] }) => s.transform[2] < LOD_ZOOM;

function PortLabel({ port, side, connected, showMissing }: { port: NodePort; side: 'in' | 'out'; connected: boolean; showMissing: boolean }) {
  const { t } = useI18n();
  const title = `${portLabel(t, port.name)} · ${portTypeLabel(t, port.type)}${port.multiple ? ` · ${t('wf.port.multiple')}` : ''}${!port.required && side === 'in' ? ` · ${t('common.optional')}` : ''}`;
  return (
    <span className={`wf-port-label ${side} ${showMissing && !connected && port.required ? 'missing' : ''}`} title={title}>
      <span className={`wf-port-dot pt-${port.type}`} aria-hidden />
      <span className="ellip">{portLabel(t, port.name)}</span>
      {port.multiple && <span className="wf-port-multi">×n</span>}
      {!port.required && side === 'in' && <span className="wf-port-opt">{t('wf.port.optionalShort')}</span>}
    </span>
  );
}

function WfNodeViewInner({ id, data, selected }: NodeProps<WfNode>) {
  const { t, fmt } = useI18n();
  const ctx = useCanvas();
  const lod = useStore(lodSelector);
  const def = ctx.defs.get(data.nodeType);
  const errors = ctx.errorsByNode.get(id) ?? [];
  const run = ctx.nodeRuns?.get(id);
  const status = run?.status;
  const conn = ctx.connectedInputs.get(id);
  const revision = useRevision(data.nodeType === 'model_input' ? data.params.revision_id : null);
  const inputs = def?.inputs ?? [];
  const outputs = def?.outputs ?? [];
  const rows = Math.max(inputs.length, outputs.length);
  const lines = paramLines(t, fmt, def, data.params, { lookups: ctx.lookups, revision }).slice(0, 3);
  const connectable = !ctx.readOnly && ctx.mode === 'edit';
  const isRun = ctx.mode === 'run';
  const hasUpstream = !!conn?.size;
  // Zoomed out: the result in one line (run), or the first setting / node type (edit).
  const line = isRun ? compactLine(t, fmt, run, nodeSummary(run)) : (lines[0]?.value ?? def?.category ?? '');

  return (
    <div
      className={[
        'wf-node',
        `cat-${def?.category ?? 'unknown'}`,
        selected ? 'selected' : '',
        errors.length ? 'has-errors' : '',
        status ? `ns-${status}` : '',
        isRun ? 'is-run' : '',
        lod ? 'lod' : '',
      ].join(' ')}
      data-node-id={id}
    >
      {isRun && ctx.onOpenNode && (
        <div className="wf-nd-tools nodrag">
          {ctx.onOpenNode && (
            <button type="button" title={t('cv.tools.details')} aria-label={t('cv.tools.details')} onClick={() => ctx.onOpenNode?.(id)}>
              <ChevronRight aria-hidden />
            </button>
          )}
        </div>
      )}
      <header className="wf-node-head">
        <span className="wf-node-ico">
          <NodeTypeIcon type={data.nodeType} />
        </span>
        <span className="wf-node-titles">
          <span className="wf-node-title ellip">{nodeDisplayName(t, { type: data.nodeType, label: data.label })}</span>
          <span className="wf-node-sub mono ellip">{id}</span>
        </span>
        {def?.uses_ai && (
          <span className="wf-ai-badge" title={t('wf.ai.badgeTitle')}>
            <Sparkles aria-hidden /> AI
          </span>
        )}
        {errors.length > 0 && (
          <span className="wf-err-badge" title={errors.map((e) => graphErrorText(t, e, data.nodeType)).join('\n')} aria-label={t('wf.issuesCount', { n: errors.length })}>
            <TriangleAlert aria-hidden />
            {errors.length}
          </span>
        )}
        {isRun && status && <NodeStatusChip status={status} compact />}
      </header>
      {!def && <div className="wf-node-unknown">{t('wf.unknownNodeType', { type: data.nodeType })}</div>}
      {rows > 0 && (
        <div className="wf-ports">
          {Array.from({ length: rows }, (_, i) => {
            const pin = inputs[i];
            const pout = outputs[i];
            return (
              <div className="wf-port-row" key={i}>
                {pin ? (
                  <>
                    <Handle type="target" position={Position.Left} id={pin.name} className={`wf-handle pt-${pin.type}`} isConnectable={connectable} />
                    <PortLabel port={pin} side="in" connected={!!conn?.has(pin.name)} showMissing={ctx.mode === 'edit'} />
                  </>
                ) : (
                  <span />
                )}
                {pout ? (
                  <>
                    <PortLabel port={pout} side="out" connected showMissing={false} />
                    <Handle type="source" position={Position.Right} id={pout.name} className={`wf-handle pt-${pout.type}`} isConnectable={connectable} />
                  </>
                ) : (
                  <span />
                )}
              </div>
            );
          })}
        </div>
      )}
      {lines.length > 0 && (
        <dl className="wf-node-params">
          {lines.map((l) => (
            <div key={l.name} className={l.isDefault ? 'is-default' : ''}>
              <dt>{l.label}</dt>
              <dd className="ellip" title={l.value}>
                {l.value}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {isRun && run && (
        <>
          <ResultBlock nr={run} runs={ctx.nodeRuns} />
          <StateBlock nr={run} hasUpstream={hasUpstream} onOpen={ctx.onOpenNode ? () => ctx.onOpenNode?.(id) : undefined} />
          <NodeFooter nr={run} nodeType={data.nodeType} usesAi={!!def?.uses_ai} />
        </>
      )}
      {lod && (
        <div className="wf-node-cmp ellip" title={line}>
          {line}
        </div>
      )}
    </div>
  );
}

export const WfNodeView = memo(WfNodeViewInner);

function FrameViewInner({ data }: NodeProps<FrameNode>) {
  const { t } = useI18n();
  const color = groupColor(data.group.color);
  return (
    <div className={`wf-frame gc-${color} ${data.active ? 'active' : ''}`}>
      <div className="wf-frame-head">
        <span className="wf-frame-dot" aria-hidden />
        <span className="ellip">{data.group.label ? groupDisplayLabel(t, data.group.label) : t('wf.group.untitled')}</span>
      </div>
    </div>
  );
}

export const FrameView = memo(FrameViewInner);

export const NODE_TYPES = { wf: WfNodeView, frame: FrameView };
