// Localized labels for node types, ports, params, statuses, steps and errors.
// Every dynamic key falls back to readable text when the dictionary has no entry
// (e.g. a node type added on the server before the web app knows it).

import type { GraphError, NodeStatus, PortType, RunStatus } from '../../api/types';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import type { TFunction } from '../../i18n/I18nProvider';

function dyn(t: TFunction, key: string, fallback: string, vars?: Record<string, string | number>): string {
  return hasKey(key) ? t(key as MessageKey, vars) : fallback;
}

function humanize(s: string): string {
  const x = s.replace(/_/g, ' ').trim();
  return x ? x[0].toUpperCase() + x.slice(1) : s;
}

export const nodeTypeLabel = (t: TFunction, type: string) => dyn(t, `wf.node.${type}`, humanize(type));
export const nodeTypeDoc = (t: TFunction, type: string, fallback: string) => dyn(t, `wf.nodeDoc.${type}`, fallback);
export const portLabel = (t: TFunction, name: string) => dyn(t, `wf.port.${name}`, humanize(name));
export const portTypeLabel = (t: TFunction, type: PortType | string) => dyn(t, `wf.ptype.${type}`, type);
export const paramLabel = (t: TFunction, name: string, title?: string) => dyn(t, `wf.param.${name}`, title || humanize(name));
export const categoryLabel = (t: TFunction, c: string) => dyn(t, `wf.cat.${c}`, humanize(c));
export const nodeStatusLabel = (t: TFunction, s: NodeStatus | string) => dyn(t, `wf.nstatus.${s}`, humanize(s));
export const runStatusLabel = (t: TFunction, s: RunStatus | string) => dyn(t, `wf.rstatus.${s}`, humanize(s));
export const stepKindLabel = (t: TFunction, k: string) => dyn(t, `wf.step.${k}`, humanize(k));
export const enumLabel = (t: TFunction, param: string, value: string) => dyn(t, `wf.enum.${param}.${value}`, dyn(t, `wf.enum.${value}`, value));
export const actionLabel = (t: TFunction, a: string) => dyn(t, `wf.action.${a}`, humanize(a));
export const stageLabel = (t: TFunction, stage: string) => dyn(t, `wf.stage.${stage}`, humanize(stage));

const TEMPLATE_GROUP_LABELS: Record<string, string> = { Design: 'wf.groupLabel.design', Manufacturing: 'wf.groupLabel.manufacturing' };

/** Template groups ship with English labels; show them localized until the user renames them. */
export function groupDisplayLabel(t: TFunction, label: string): string {
  const k = TEMPLATE_GROUP_LABELS[label];
  return k ? dyn(t, k, label) : label;
}

export function nodeDisplayName(t: TFunction, node: { type: string; label?: string | null }): string {
  return node.label?.trim() || nodeTypeLabel(t, node.type);
}

/** Friendly text for a graph validation error; the server message stays available as a tooltip. */
export function graphErrorText(t: TFunction, e: GraphError, nodeType?: string): string {
  const vars = { port: e.port ? portLabel(t, e.port) : '', param: e.param ? paramLabel(t, e.param) : '' };
  if (e.code === 'missing_input') {
    if (e.port) return t('wf.verr.missing_input_port', vars);
    if (nodeType === 'model_3d') return t('wf.verr.model_3d_inputs');
    if (nodeType === 'drawing_reconstruct') return t('wf.verr.drawing_reconstruct_inputs');
    if (nodeType === 'process_plan') return t('wf.verr.model_or_spec');
    return t('wf.verr.missing_input');
  }
  if (e.code === 'missing_param') {
    if (e.param === 'text') return t('wf.verr.missing_text');
    if (e.param === 'file_version_id') return t('wf.verr.missing_file');
    if (e.param === 'revision_id') return t('wf.verr.missing_revision');
    return t('wf.verr.missing_param', vars);
  }
  if (e.code === 'ambiguous_input' && nodeType === 'drawing_reconstruct') return t('wf.verr.ambiguous_drawing_input');
  return dyn(t, `wf.verr.${e.code}`, e.message, vars);
}

/** Friendly text for a node run error code (handlers.py NodeFailure codes, provider codes). */
export function nodeErrorText(t: TFunction, code: string | null | undefined, message: string | null | undefined): string {
  if (code && hasKey(`wf.nerr.${code}`)) return t(`wf.nerr.${code}` as MessageKey);
  if (code && hasKey(`error.${code}`)) return t(`error.${code}` as MessageKey);
  return message || t('wf.nerr.generic');
}
