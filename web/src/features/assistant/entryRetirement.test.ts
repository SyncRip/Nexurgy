import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import type { AiConversationDetail, AiMessage, AiPreflight, AiRef } from '../../api/assistant';
import { ToastProvider } from '../../components/ui/Toast';
import { I18nProvider } from '../../i18n/I18nProvider';
import { MessageList } from './MessageList';
import { PreflightNotice } from './PreflightNotice';

vi.mock('./AssistantProvider', () => ({ useAssistantSession: () => ({ store: {}, capabilities: null,
  drafts: { key: (id: string) => `test.${id}`, read: () => ({ requestId: null }), persistent: true },
}) }));

const message = (kind: AiMessage['kind'], payload: Record<string, unknown> = {}, text = '') => ({
  id: kind, seq: kind === 'request' ? 1 : 2, role: kind === 'request' ? 'user' : 'assistant', kind, text,
  operation_id: null, target: null, references: [], ref_status: {}, payload, created_at: '2026-10-16T00:00:00Z',
}) as AiMessage;

function messages(items: AiMessage[], archived = false): string {
  const detail = { conversation: { archived, constraints: [] }, messages: items, operations: [] } as unknown as AiConversationDetail;
  return renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null, h(ToastProvider, null,
    h(MessageList, { detail, onPrefill: () => {} })))));
}

describe('retired operation selectors keep history and human follow-ups', () => {
  it('renders old choices and their reason as a readable record without action buttons', () => {
    const html = messages([message('choice', { choices: ['model_create', 'drawing_intake', 'legacy_operation', null, 'model_create'], reason: 'no_target' }, 'Earlier choice prompt')]);
    expect(html).toContain('Earlier choice prompt');
    expect(html).toContain('No target was set for this earlier request.');
    expect(html).toContain('New model'); expect(html).toContain('Drawing analysis'); expect(html).toContain('legacy_operation');
    expect(html).toContain('Earlier operation choices.');
    expect(html).not.toContain('<button'); expect(html).not.toContain('as-choices');
  });

  it('keeps the original exact-version snapshot readable with a historical choice', () => {
    const request = message('request', {}, 'Original request');
    request.target = { key: 'part_revision:r1', kind: 'part_revision', object_id: 'r1', title: 'Original bracket',
      version_label: 'R1', summary: '', source_path: '/parts/p1/model?rev=r1' } as AiRef;
    const html = messages([request, message('choice', { choices: ['model_modify'], request_seq: 1 })], true);
    expect(html).toContain('Original request');
    expect(html.match(/as-ref-name">Original bracket/g)).toHaveLength(2);
    expect(html.match(/as-ref-ver mono">R1/g)).toHaveLength(2);
    expect(html).not.toContain('<button');
  });

  it('keeps ordinary answer follow-ups while ignoring the old suggested operation action', () => {
    const html = messages([message('answer', { suggested_operation: 'model_create', follow_ups: ['Explain the dimensions', '', null] }, 'Saved answer')]);
    expect(html).toContain('Saved answer'); expect(html).toContain('Explain the dimensions');
    expect(html.match(/<button/g)).toHaveLength(1);
    expect(html).toContain('Choose the next step'); expect(html).toContain('Click to run');
    expect(html).toContain('Run this option'); expect(html).toContain('data-followup-state="ready"');
    expect(html).not.toContain('Use operation:');
    expect(messages([message('answer', { suggested_operation: 'model_create' }, 'Only suggestion')])).not.toContain('<button');
    expect(messages([message('answer', { follow_ups: ['Explain the dimensions'] })], true)).not.toContain('<button');
  });

  it('explains unavailable linked context and disables a fresh option without falling back to empty refs', () => {
    const answer = { ...message('answer', { follow_ups: ['Explain the dimensions'] }), operation_id: 'missing-owner' };
    const html = messages([answer]);
    expect(html).toContain('data-followup-state="unavailable" disabled=""');
    expect(html).toContain('Context unavailable');
    expect(html).toContain('Refresh the conversation or send a new request');
  });
});

describe('preflight routing labels', () => {
  const base: AiPreflight = { operation: 'ask', requested_operation: 'auto', operation_source: 'main_agent',
    runtime: 'orchestrator', target: { type: 'none', label: '', version: null, id: null, key: null },
    references: [], missing: [], policy: 'standard', policy_source: 'project', will_call_model: true };
  const render = (patch: Partial<AiPreflight>) => renderToStaticMarkup(h(MemoryRouter, null, h(I18nProvider, null,
    h(PreflightNotice, { preflight: { ...base, ...patch }, projectId: 'p1', busy: false, attachShown: false, onAdd: () => {} }))));

  it('describes the main assistant and read-only fallback without a predicted operation selector', () => {
    expect(render({})).toContain('Will run: AI assistant');
    expect(render({ operation_source: 'fallback', runtime: undefined })).toContain('Will run: read-only answer');
    expect(render({})).not.toContain('recognised from the text');
  });

  it('retains explicit operation labels and safely reads historical classified payloads', () => {
    expect(render({ operation_source: 'explicit', operation: 'drawing_intake', runtime: undefined })).toContain('Drawing analysis');
    const old = render({ operation_source: 'classified', operation: null, choices: ['model_create'] });
    expect(old).not.toContain('<button'); expect(old).not.toContain('choose the operation');
  });
});
