import { describe, expect, it } from 'vitest';
import type { AiConversationDetail, AiMessage, AiOperation } from '../../api/assistant';
import { initialAssemblyOperation, positionMessages, trackMessageScroll, type MessageScrollPosition } from './MessageList';

const operation = (id: string, patch: Partial<AiOperation> = {}): AiOperation => ({
  id, status: 'succeeded', result: { kind: 'assembly', cad_result: { object_type: 'assembly', object_id: 'cart', version_id: id } }, ...patch,
}) as AiOperation;
const message = (kind: AiMessage['kind'], operation_id: string | null = null): AiMessage => ({
  kind, operation_id, role: kind === 'request' ? 'user' : 'assistant',
}) as AiMessage;
const detail = (messages: AiMessage[], operations: AiOperation[]): Pick<AiConversationDetail, 'messages' | 'operations'> => ({ messages, operations });

describe('initial conversation result selection', () => {
  it('selects the newest saved assembly in the latest exchange, regardless of operation array order', () => {
    expect(initialAssemblyOperation(detail([
      message('request'), message('operation', 'old'), message('operation', 'new'), message('answer', 'new'),
    ], [operation('new'), operation('old')]))).toBe('new');
  });

  it('does not jump over a newer user request or failure to show an older result', () => {
    expect(initialAssemblyOperation(detail([message('operation', 'old'), message('request')], [operation('old')]))).toBeNull();
    expect(initialAssemblyOperation(detail([message('operation', 'old'), message('operation', 'failed')], [
      operation('old'), operation('failed', { status: 'failed' }),
    ]))).toBeNull();
  });

  it('keeps live work at the latest message and requires a saved assembly', () => {
    const messages = [message('request'), message('operation', 'saved')];
    expect(initialAssemblyOperation(detail(messages, [operation('saved'), operation('live', { status: 'running' })]))).toBeNull();
    expect(initialAssemblyOperation(detail(messages, [operation('saved', { result: null })]))).toBeNull();
  });
});

function viewport() {
  let top = 0;
  const events = new EventTarget();
  return {
    scrollHeight: 1000, clientHeight: 302,
    addEventListener: events.addEventListener.bind(events),
    removeEventListener: events.removeEventListener.bind(events),
    dispatchEvent: events.dispatchEvent.bind(events),
    get scrollTop() { return top; },
    set scrollTop(value: number) { top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); },
    getBoundingClientRect: () => ({ top: 258 }) as DOMRect,
  };
}
function savedPosition(host: ReturnType<typeof viewport>, offset = 400) {
  return () => ({ top: 258 + offset - host.scrollTop, previewTop: 258 + offset + 40 - host.scrollTop, previewBottom: 258 + offset + 220 - host.scrollTop });
}

describe('conversation entry scroll position', () => {
  it('uses the actual scroll host to resume following at the bottom and stops listening on cleanup', () => {
    const host = viewport();
    const state: MessageScrollPosition = { conversationId: 'conversation', follow: false };
    const stop = trackMessageScroll(host, state);
    host.scrollTop = host.scrollHeight;
    host.dispatchEvent(new Event('scroll'));
    expect(state.follow).toBe(true);
    host.scrollHeight += 100;
    positionMessages(host, state, 'conversation');
    expect(host.scrollTop).toBe(798);
    host.scrollTop = 80;
    host.dispatchEvent(new Event('scroll'));
    expect(state.follow).toBe(false);
    stop();
    host.scrollTop = host.scrollHeight;
    host.dispatchEvent(new Event('scroll'));
    expect(state.follow).toBe(false);
  });

  it('brings the saved model heading and preview into a short mobile viewport', () => {
    const host = viewport();
    const state: MessageScrollPosition = { conversationId: null, follow: true };
    positionMessages(host, state, 'conversation', savedPosition(host));
    const bounds = savedPosition(host)();
    expect(bounds.top).toBe(270);
    expect(bounds.previewTop).toBeGreaterThanOrEqual(258);
    expect(bounds.previewBottom).toBeLessThanOrEqual(560);
    expect(state.follow).toBe(false);
  });

  it('retains the ordinary bottom position when the result is already visible', () => {
    const host = viewport();
    const state: MessageScrollPosition = { conversationId: null, follow: true };
    positionMessages(host, state, 'conversation', savedPosition(host, 750));
    expect(host.scrollTop).toBe(698);
    expect(state.follow).toBe(true);
  });

  it('does not pull a positioned or manually scrolled reader around on refresh', () => {
    const host = viewport();
    const state: MessageScrollPosition = { conversationId: null, follow: true };
    positionMessages(host, state, 'conversation', savedPosition(host));
    const initial = host.scrollTop;
    host.scrollHeight += 300;
    positionMessages(host, state, 'conversation', savedPosition(host));
    expect(host.scrollTop).toBe(initial);
    host.scrollTop = 80;
    state.follow = false;
    positionMessages(host, state, 'conversation', savedPosition(host));
    expect(host.scrollTop).toBe(80);
  });

  it('follows new messages at the bottom and resets entry behavior when switching conversations', () => {
    const host = viewport();
    const state: MessageScrollPosition = { conversationId: null, follow: true };
    positionMessages(host, state, 'live');
    expect(host.scrollTop).toBe(698);
    host.scrollHeight += 100;
    positionMessages(host, state, 'live');
    expect(host.scrollTop).toBe(798);
    state.follow = false;
    positionMessages(host, state, 'saved', savedPosition(host));
    expect(savedPosition(host)().top).toBe(270);
  });
});
