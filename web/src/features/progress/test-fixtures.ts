import type { ProgressStage, ProgressUnit, RunProgress } from '../../api/progress';

export const calls = (count: number, extra: Partial<NonNullable<ProgressUnit['calls']>> = {}): NonNullable<ProgressUnit['calls']> => ({
  count,
  failed: 0,
  repairs: 0,
  tokens_in: 0,
  tokens_out: 0,
  latency_ms: 0,
  started_at: null,
  purposes: [],
  last_error: null,
  details: [],
  ...extra,
});

export const unit = (key: string, status: string, extra: Partial<ProgressUnit> = {}): ProgressUnit => ({
  key,
  kind: key.split(':')[0],
  title: null,
  quantity: null,
  status,
  attempts: null,
  revision: null,
  calls: null,
  decisions: [],
  error: null,
  ...extra,
});

export const stage = (node_id: string, status: string, units: ProgressUnit[] = [], extra: Partial<ProgressStage> = {}): ProgressStage => ({
  node_id,
  node_type: node_id,
  label: null,
  status,
  attempt: 1,
  started_at: '2026-10-17T13:01:00Z',
  finished_at: status === 'running' ? null : '2026-10-17T13:02:00Z',
  units,
  units_truncated: false,
  calls: calls(0),
  decisions: [],
  error: null,
  ...extra,
});

export const run = (status: string, stages: ProgressStage[], totals: Partial<RunProgress['totals']> = {}): RunProgress => ({
  run_id: 'r',
  status,
  policy: 'turbo',
  simulated: false,
  started_at: '2026-10-17T13:01:00Z',
  finished_at: status === 'running' ? null : '2026-10-17T13:07:00Z',
  totals: { calls: 0, failed_calls: 0, tokens_in: 0, tokens_out: 0, assumptions: 0, units: 0, units_built: 0, ...totals },
  truncated: false,
  stages,
});
