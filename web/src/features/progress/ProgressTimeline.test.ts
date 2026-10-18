import { createElement, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ProgressCall } from '../../api/progress';
import type { AiCallRecord } from '../../api/types';
import { I18nProvider } from '../../i18n/I18nProvider';
import { CallGroups, groupCalls } from '../cad/AiCallLedger';
import { ProgressTimeline } from './ProgressTimeline';
import { calls, run, stage, unit } from './test-fixtures';

const html = (element: ReactElement, language = 'zh') => {
  vi.stubGlobal('localStorage', { getItem: () => language });
  return renderToStaticMarkup(createElement(I18nProvider, null, element));
};
const text = (markup: string) => markup.replace(/<[^>]+>/g, ' ');
afterEach(() => vi.unstubAllGlobals());

const call = (extra: Partial<ProgressCall> = {}): ProgressCall => ({
  id: 'call-1', purpose: 'modeling', attempt: 1, status: 'error', simulated: false,
  started_at: '2026-10-17T12:00:00Z', latency_ms: 1400, tokens_in: null, tokens_out: 44,
  request_id: 'provider-request-1', error: { code: 'bad_response', failure_class: 'unparsable', diagnostic_code: 'output_truncated' }, ...extra,
});

describe('rendered progress', () => {
  it('keeps the real specialist stages within a manager-coordinated assembly workflow', () => {
    const p = run('running', [
      stage('spec', 'succeeded', [], { node_type: 'engineering_spec' }),
      stage('assembly', 'running', [], { node_type: 'assembly_from_spec' }),
      stage('drawing', 'pending', [], { node_type: 'assembly_drawing', started_at: null, finished_at: null }),
      stage('delivery', 'pending', [], { node_type: 'delivery_package', started_at: null, finished_at: null }),
    ]);
    const markup = html(createElement(ProgressTimeline, { progress: p, variant: 'full' }), 'en');
    const rows = markup.match(/<li class="prog-stage[^>]*>[\s\S]*?<\/li>/g) ?? [];
    expect(rows).toHaveLength(4);
    for (const [index, role, status] of [[0, 'design', 'Succeeded'], [1, 'assembly', 'Running'],
      [2, 'drawing', 'Pending'], [3, 'manager', 'Pending']] as const) {
      expect(rows[index]).toContain(`data-engineering-role="${role}"`);
      expect(rows[index]).toContain(status);
    }
  });

  it('shows each actual stage role beside its real state, leaving human and custom stages alone', () => {
    const p = run('partial', [
      stage('process', 'failed', [], { node_type: 'assembly_process_plan', error: { code: 'missing_geometry' } }),
      stage('quote', 'pending', [], { node_type: 'assembly_quote', started_at: null, finished_at: null }),
      stage('drawing', 'skipped', [], { node_type: 'assembly_drawing', started_at: null, finished_at: null }),
      stage('review', 'waiting_human', [], { node_type: 'human_review' }),
      stage('custom', 'pending', [], { node_type: 'business_task', label: 'Custom model engineer', started_at: null, finished_at: null }),
    ]);
    const markup = html(createElement(ProgressTimeline, { progress: p, variant: 'full' }), 'en');
    const rows = markup.match(/<li class="prog-stage[^>]*>[\s\S]*?<\/li>/g) ?? [];
    expect(rows).toHaveLength(5);
    expect(rows[0]).toContain('data-engineering-role="process"');
    expect(rows[0]).toContain('Failed');
    expect(rows[0]).toContain('class="prog-err"');
    expect(rows[1]).toContain('data-engineering-role="quote"');
    expect(rows[1]).toContain('Pending');
    expect(rows[2]).toContain('data-engineering-role="drawing"');
    expect(rows[2]).toContain('Skipped');
    expect(rows[3]).toContain('Waiting for a person');
    expect(rows[4]).toContain('Custom model engineer');
    for (const row of rows.slice(3)) expect(row).not.toContain('data-engineering-role');
    for (const row of rows) expect(row).not.toContain('Succeeded');
    const chinese = html(createElement(ProgressTimeline, { progress: p, variant: 'full' }));
    expect(chinese).toContain('工艺拆解工程师');
    expect(chinese).toContain('报价工程师');
    expect(chinese).not.toContain('team.role.');
  });

  it('renders legacy modeling text in a standard run without inventing a Turbo decision or missing-field problems', () => {
    const p = run('failed', [stage('assembly', 'failed', [unit('definition:D-03', 'built', {
      decisions: [{ kind: 'assumption', text: '车轴轴线沿 Z 轴，底面置于 Z=0，中心位于原点', subject: null, field: null,
        adopted: null, author: null, state: null, confirmation: null, problems: null, owner: { type: 'model_revision', id: 'revision-1', rev_no: 1 } }],
    })])], { assumptions: 1 });
    p.policy = 'standard';
    const markup = html(createElement(ProgressTimeline, { progress: p, variant: 'full' }));
    const content = text(markup);
    expect(content).toContain('建模说明');
    expect(content).toContain('车轴轴线沿 Z 轴，底面置于 Z=0，中心位于原点');
    for (const invented of ['Turbo', 'AI 判断', '没有写明判断对象', '未给出采用值']) expect(content).not.toContain(invented);
    expect(markup).not.toContain('data-problems');
    expect(content).not.toContain('1 条假设');
    expect(text(html(createElement(ProgressTimeline, { progress: p, variant: 'full' }), 'en'))).toContain('Modeling notes');
  });

  it('keeps explicit correction and problem records in the shared decision view even if their old fields are incomplete', () => {
    const p = run('failed', [stage('model_3d', 'failed', [unit('model', 'failed', {
      decisions: [{ kind: 'assumption', text: 'Original modeling note', confirmation: { decision: 'corrected' }, problems: ['no_subject'] }],
    })])]);
    const markup = html(createElement(ProgressTimeline, { progress: p }));
    expect(markup).toContain('data-state="corrected"');
    expect(markup).toContain('data-problems="1"');
    expect(text(markup)).toContain('已由人更正');
  });

  it('shows individual failed/repair calls, the safe truncation explanation and unknown provider usage', () => {
    const history = calls(2, { failed: 1, repairs: 1, tokens_in: null, tokens_out: 88, details: [call(), call({ id: 'call-2', purpose: 'modeling_repair', attempt: 2, status: 'ok', error: null })] });
    const p = run('succeeded', [stage('model_3d', 'succeeded', [unit('model', 'built', { calls: history })])], { calls: 2, tokens_in: null, tokens_out: 88 });
    const zh = text(html(createElement(ProgressTimeline, { progress: p, variant: 'full' })));
    expect(zh).toContain('Token 用量未知');
    expect(zh).toContain('未知 / 44');
    expect(zh).toContain('模型回复被截断');
    expect(zh).toContain('建模修复');
    expect(zh).toContain('第 2 次尝试');
    expect(zh).toContain('provider-request-1');
    expect(zh).toContain('1 次调用失败后已恢复');
    const en = text(html(createElement(ProgressTimeline, { progress: p, variant: 'full' }), 'en'));
    expect(en).toContain('Token usage unknown');
    expect(en).toContain('The model’s reply was cut off');
    expect(en).toContain('Modeling repair');
  });

  it('uses the shared assumption view with correction, trigger, unit, impact and recorded problems intact', () => {
    const p = run('failed', [stage('model_3d', 'failed', [unit('model', 'failed', {
      decisions: [{ kind: 'assumption', subject: 'shaft', field: 'length', adopted: 30, unit: 'mm',
        text: 'Fits the available envelope', trigger: 'conflict', affected: ['wheel-clearance'],
        state: 'corrected', problems: ['no_basis'], confirmation: { decision: 'corrected' },
        owner: { type: 'model_revision', id: 'revision-1', rev_no: 1 } }],
    })])]);
    const markup = html(createElement(ProgressTimeline, { progress: p }));
    expect(markup).toContain('data-state="corrected"');
    expect(markup).toContain('data-problems="1"');
    const content = text(markup);
    for (const expected of ['30 mm', 'Fits the available envelope', 'wheel-clearance', '已由人更正', '冲突', '没有给出理由', 'length']) expect(content).toContain(expected);
  });

  it('shows saved modeling operations and keeps simulated calls out of token usage', () => {
    const p = run('succeeded', [stage('model_3d', 'succeeded', [unit('model', 'built', {
      revision: { id: 'revision-1', part_id: 'part-1', label: 'R1', status: 'draft', size: [40, 20, 10], operations: [{ op: 'box', count: 1 }, { op: 'hole', count: 4 }] },
      calls: calls(1, { details: [call({ simulated: true, tokens_in: 998877, tokens_out: 665544 })] }),
    })])]);
    const content = text(html(createElement(ProgressTimeline, { progress: p, variant: 'full' })));
    expect(content).toContain('建模操作：长方体 ×1 · 孔 ×4');
    expect(content).toContain('不计入用量（模拟）');
    expect(content).not.toContain('998877');
    expect(content).not.toContain('665544');
  });

  it('reveals a late failed unit in a compact card and labels generic done as completed calls', () => {
    const units = Array.from({ length: 14 }, (_, i) => unit(`definition:D-${i}`, i === 13 ? 'failed' : 'built', { title: `part-${i}` }));
    units.push(unit('spec_chunk:1', 'done', { calls: calls(1) }));
    const p = run('partial', [stage('assembly', 'failed', units, { error: { code: 'bad_response', diagnostic_code: 'output_truncated' } })]);
    const compact = html(createElement(ProgressTimeline, { progress: p }));
    expect(compact).toContain('aria-expanded="true"');
    expect(text(compact)).toContain('part-13');
    const full = text(html(createElement(ProgressTimeline, { progress: p, variant: 'full' })));
    expect(full).toContain('调用完成');
    expect(full).toContain('模型回复被截断');
  });
});

const ledgerCall = (id: string, unitKey: string, node = 'assembly'): AiCallRecord => ({
  id, unit: unitKey, node_id: node, node_run_id: `${node}-run`, agent_execution_id: null, purpose: 'modeling', provider: 'gemini', model_id: 'model',
  status: 'ok', error_code: null, attempt: 1, failure_class: null, latency_ms: 2000, tokens_in: null, tokens_out: 10,
  provider_request_id: `request-${id}`, simulated: false, created_at: '2026-10-17T12:00:00Z',
});

describe('model call ledger groups', () => {
  it('collects interleaved attempts under their visible node and unit without merging different stages', () => {
    const records = [ledgerCall('a', 'definition:D-01'), ledgerCall('b', 'definition:D-02'), ledgerCall('c', 'definition:D-01'), ledgerCall('d', 'definition:D-01', 'other-stage')];
    records[2].attempt = 2;
    expect(groupCalls(records).map((g) => g.calls.map((c) => c.id))).toEqual([['a', 'c'], ['b'], ['d']]);
    const markup = html(createElement(CallGroups, { calls: records }));
    expect(markup.match(/scope="rowgroup"/g)).toHaveLength(3);
    const content = text(markup);
    expect(content).toContain('definition:D-01');
    expect(content).toContain('definition:D-02');
    expect(content).toContain('other-stage');
    expect(content).toContain('第 2 次尝试');
    expect(content).toContain('未知 / 10');
  });
});
