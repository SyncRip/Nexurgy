import { request } from './client';

export const TODO_SOURCES = ['engineering_approval', 'agent_confirmation', 'question', 'spec_question', 'turbo_assumption'] as const;
export type TodoSource = (typeof TODO_SOURCES)[number];
export interface TodoItem {
  key: string;
  source: TodoSource;
  source_id: string;
  project: { id: string; code: string; name: string };
  object: { type: 'part' | 'run' | 'conversation' | 'engineering_spec' | 'assembly'; id: string; label: string };
  version: { type: 'model_revision' | 'drawing' | 'intake' | 'process_plan' | 'quote' | 'engineering_spec_version' | 'assembly_revision' | 'workflow_run' | 'ai_operation'; id: string; label: string | null } | null;
  title: string;
  reason: { code: string; summary: string; count: number };
  created_at: string;
  updated_at: string;
  target_hash: string | null;
  location: { href: string; request_id?: string; conversation_id?: string; operation_id?: string; step?: number; run_id?: string; node_run_id?: string; node_id?: string; spec_version_id?: string; question_ids?: string[]; assumption_id?: string };
  allowed_actions: ['open'];
}
export interface TodosOut { items: TodoItem[]; total: number; counts: Record<TodoSource, number>; next_cursor: string | null; as_of: string }
export const listTodos = (query: { project_id?: string; cursor?: string; limit?: number } = {}, o: { signal?: AbortSignal } = {}) =>
  request<TodosOut>('/todos', { query, signal: o.signal });

/** Aggregate links can navigate within the app, never execute or redirect outside it. */
export function todoHref(item: TodoItem): string | null {
  const href = item.location.href;
  if (!href.startsWith('/') || href.startsWith('//') || /[\\\u0000-\u0020]/.test(href)) return null;
  const url = new URL(href, 'https://partroute.invalid');
  if (item.location.question_ids?.length) url.searchParams.set('questions', item.location.question_ids.join(','));
  return `${url.pathname}${url.search}${url.hash}`;
}
