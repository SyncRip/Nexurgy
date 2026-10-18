// One-line, localized summaries of node parameters (node cards, run dialog).

import type { ModelRevision, WorkflowNodeType } from '../../api/types';
import type { Formatters } from '../../i18n/format';
import type { TFunction } from '../../i18n/I18nProvider';
import { enumLabel, paramLabel } from './labels';
import type { Lookups } from './lookups';
import { effectiveParam, paramSpecs } from './model';

export interface ParamLine {
  name: string;
  label: string;
  value: string;
  /** True when the value is the default (shown fainter). */
  isDefault: boolean;
}

export function clip(s: string, n = 64): string {
  const one = s.replace(/\s+/g, ' ').trim();
  return one.length > n ? `${one.slice(0, n - 1)}…` : one;
}

export function shortId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) : id;
}

const UNITS: Record<string, string> = { allowance_mm: 'mm', margin_pct: '%', tax_pct: '%' };

export function formatParamValue(
  t: TFunction,
  fmt: Formatters,
  name: string,
  value: unknown,
  ctx: { lookups?: Lookups; revision?: ModelRevision | null },
): string {
  if (value === null || value === undefined || value === '') {
    if (name === 'model_config_id') return t('wf.param.defaultModel');
    return '';
  }
  if (name === 'file_version_id') {
    const info = ctx.lookups?.fileVersion(value);
    return info ? `${info.file.name} · v${info.version.version}` : shortId(String(value));
  }
  if (name === 'revision_id') {
    return ctx.revision && ctx.revision.id === value ? t('wf.revisionLabel', { n: ctx.revision.rev_no }) : shortId(String(value));
  }
  if (name === 'model_config_id') {
    const c = ctx.lookups?.modelConfig(value);
    return c ? c.label : shortId(String(value));
  }
  if (name === 'scale') return value === 'auto' ? t('wf.enum.scale.auto') : fmt.number(Number(value));
  if (name === 'section') {
    if (value === 'none') return t('cad.dw.section.none');
    if (value && typeof value === 'object') {
      const sp = value as { plane?: string; offset?: number };
      if (sp.plane === 'XZ' || sp.plane === 'YZ') return `${sp.plane} @ ${fmt.number(Number(sp.offset ?? 0))} mm`;
    }
    return t('cad.dw.section.auto');
  }
  if (name === 'drawing_id') return shortId(String(value));
  if (name === 'validity_days') return t('wf.days', { n: fmt.number(Number(value)) });
  if (Array.isArray(value)) {
    if (value.every((v) => typeof v === 'number')) return value.map((v) => fmt.number(v as number)).join(' / ');
    return value.map((v) => enumLabel(t, name, String(v))).join(', ');
  }
  if (typeof value === 'number') return `${fmt.number(value)}${UNITS[name] ? ` ${UNITS[name]}` : ''}`;
  if (typeof value === 'string') {
    if (name === 'stage') return enumLabel(t, name, value);
    return clip(value);
  }
  return clip(JSON.stringify(value));
}

export function paramLines(
  t: TFunction,
  fmt: Formatters,
  def: WorkflowNodeType | undefined,
  params: Record<string, unknown>,
  ctx: { lookups?: Lookups; revision?: ModelRevision | null },
): ParamLine[] {
  if (!def) return [];
  const lines: ParamLine[] = [];
  for (const spec of paramSpecs(def)) {
    const has = Object.prototype.hasOwnProperty.call(params, spec.name);
    const v = effectiveParam(spec, params);
    const text = formatParamValue(t, fmt, spec.name, v, ctx);
    if (!text) continue;
    lines.push({ name: spec.name, label: paramLabel(t, spec.name, spec.schema.title), value: text, isDefault: !has });
  }
  return lines;
}
