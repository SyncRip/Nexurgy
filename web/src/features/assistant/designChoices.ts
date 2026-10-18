import type { DesignAnswer, DesignChoiceAction, DesignChoiceBody, DesignChoiceQuestion, DesignChoiceReceipt, DesignQuestion, DesignRef } from '../../api/assistant';
import { parseDimNumber } from '../../domain/format';
import type { DraftStorage } from './drafts';

export const DESIGN_TEXT_LIMIT = 4000;
export const DESIGN_VALUE_LIMIT = 1000;
export type DesignInput = { kind: 'option'; option_id: string } | { kind: 'value'; text: string };
export interface DesignChoiceDraft {
  values: Record<string, DesignInput>;
  freeText: string;
  attemptedBody: DesignChoiceBody | null;
}
export type DesignInputError = 'number' | 'minimum' | 'maximum' | 'boolean' | 'option' | 'custom' | 'length';
export const emptyDesignDraft = (): DesignChoiceDraft => ({ values: {}, freeText: '', attemptedBody: null });

export function sameDesignRef(a: DesignRef, b: DesignRef): boolean {
  return a.schema_version === b.schema_version && a.plan_id === b.plan_id && a.version_id === b.version_id && a.version === b.version && a.sha256 === b.sha256;
}

/** A new question, version, owner or conversation gets an entirely separate form. */
export function designChoiceScope(conversationId: string, operationId: string, question: Pick<DesignChoiceQuestion, 'design_ref' | 'question_set_id'>): string {
  const ref = question.design_ref;
  return [conversationId, operationId, ref.schema_version, ref.plan_id, ref.version_id, ref.version, ref.sha256, question.question_set_id].map((part) => encodeURIComponent(String(part))).join('.');
}

/** Scalar fields use plain decimal syntax; dimension parsing must not drop a minus or a later qualifier. */
function designNumber(text: string): number | null {
  const trimmed = text.trim();
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(trimmed)) return null;
  const magnitude = parseDimNumber(trimmed.replace(/^[+-]/, ''));
  return magnitude === null ? null : trimmed.startsWith('-') ? -magnitude : magnitude;
}

/** Empty answers remain absent; zero and false are real user values. Recommendations are never fabricated answers. */
export function collectDesignAnswers(questions: readonly DesignQuestion[], values: DesignChoiceDraft['values']): { answers: DesignAnswer[]; errors: Record<string, DesignInputError> } {
  const answers: DesignAnswer[] = [];
  const errors: Record<string, DesignInputError> = Object.create(null);
  for (const q of questions) {
    const input = Object.hasOwn(values, q.id) ? values[q.id] : undefined;
    if (!input) continue;
    if (input.kind === 'option') {
      if (q.options.some((option) => option.id === input.option_id)) answers.push({ question_id: q.id, kind: 'option', option_id: input.option_id });
      else errors[q.id] = 'option';
      continue;
    }
    if (!input.text.trim()) continue;
    if (!q.allow_custom) { errors[q.id] = 'custom'; continue; }
    let value: string | number | boolean = input.text;
    if (q.input_kind === 'number') {
      const number = designNumber(value);
      if (number === null) { errors[q.id] = 'number'; continue; }
      if (q.minimum !== null && number < q.minimum) { errors[q.id] = 'minimum'; continue; }
      if (q.maximum !== null && number > q.maximum) { errors[q.id] = 'maximum'; continue; }
      value = number;
    } else if (q.input_kind === 'boolean') {
      value = value.trim();
      if (value !== 'true' && value !== 'false') { errors[q.id] = 'boolean'; continue; }
      value = value === 'true';
    } else if (value.length > DESIGN_VALUE_LIMIT) {
      errors[q.id] = 'length'; continue;
    }
    answers.push({ question_id: q.id, kind: 'value', value });
  }
  return { answers, errors };
}

/** Once attempted, replay that exact body until the server acknowledges or definitively rejects it. */
export function prepareDesignChoice(question: DesignChoiceQuestion, draft: DesignChoiceDraft, action: DesignChoiceAction, newId: () => string): DesignChoiceBody | null {
  if (!question.can_answer || !question.allowed_actions.includes(action)) return null;
  if (draft.attemptedBody) {
    const body = draft.attemptedBody;
    return body.question_set_id === question.question_set_id && sameDesignRef(body.design_ref, question.design_ref) && question.allowed_actions.includes(body.action) ? body : null;
  }
  const { answers, errors } = collectDesignAnswers(question.questions, draft.values);
  const freeText = draft.freeText;
  if (Object.keys(errors).length || freeText.length > DESIGN_TEXT_LIMIT || (action === 'customize' && !answers.length && !freeText.trim())) return null;
  return { client_request_id: newId(), design_ref: { ...question.design_ref }, question_set_id: question.question_set_id, action, answers, ...(freeText ? { free_text: freeText } : {}) };
}

export function designReceiptMatches(body: DesignChoiceBody | null, receipt: DesignChoiceReceipt | null | undefined): boolean {
  return !!body && !!receipt && receipt.client_request_id === body.client_request_id && receipt.question_set_id === body.question_set_id && receipt.action === body.action && sameDesignRef(receipt.answered_design_ref, body.design_ref);
}

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

/** Local storage is untrusted. Invalid or foreign attempts must never be sent with a retained id. */
function parseAttempt(value: unknown, question: DesignChoiceQuestion): DesignChoiceBody | null {
  const body = object(value), ref = object(body?.design_ref);
  if (!body || !ref || typeof body.client_request_id !== 'string' || !body.client_request_id || body.question_set_id !== question.question_set_id
      || !sameDesignRef(ref as unknown as DesignRef, question.design_ref)
      || !['customize', 'delegate_remaining', 'accept_recommended'].includes(String(body.action))
      || !Array.isArray(body.answers) || (body.free_text !== undefined && (typeof body.free_text !== 'string' || body.free_text.length > DESIGN_TEXT_LIMIT))) return null;
  const answers: DesignAnswer[] = [];
  const seen = new Set<string>();
  for (const raw of body.answers) {
    const a = object(raw);
    if (!a || typeof a.question_id !== 'string' || seen.has(a.question_id)) return null;
    const q = question.questions.find((item) => item.id === a.question_id);
    if (!q) return null;
    if (a.kind === 'option' && typeof a.option_id === 'string' && q.options.some((option) => option.id === a.option_id)) {
      answers.push({ question_id: a.question_id, kind: 'option', option_id: a.option_id });
    } else if (a.kind === 'value' && q.allow_custom && typeof a.value === q.input_kind && (typeof a.value !== 'number' || Number.isFinite(a.value)) && (typeof a.value !== 'string' || a.value.length <= DESIGN_VALUE_LIMIT)) {
      answers.push({ question_id: a.question_id, kind: 'value', value: a.value as string | number | boolean });
    } else return null;
    seen.add(a.question_id);
  }
  return { client_request_id: body.client_request_id, design_ref: { ...question.design_ref }, question_set_id: question.question_set_id, action: body.action as DesignChoiceAction, answers, ...(typeof body.free_text === 'string' ? { free_text: body.free_text } : {}) };
}

export function parseDesignDraft(raw: string | null, question: DesignChoiceQuestion): DesignChoiceDraft {
  if (!raw) return emptyDesignDraft();
  try {
    const draft = object(JSON.parse(raw));
    if (!draft) return emptyDesignDraft();
    const source = object(draft.values), values: DesignChoiceDraft['values'] = Object.create(null);
    for (const q of question.questions) {
      const input = object(source?.[q.id]);
      if (input?.kind === 'option' && typeof input.option_id === 'string' && q.options.some((option) => option.id === input.option_id)) values[q.id] = { kind: 'option', option_id: input.option_id };
      else if (input?.kind === 'value' && q.allow_custom && typeof input.text === 'string' && input.text.length <= DESIGN_VALUE_LIMIT) values[q.id] = { kind: 'value', text: input.text };
    }
    return { values, freeText: typeof draft.freeText === 'string' && draft.freeText.length <= DESIGN_TEXT_LIMIT ? draft.freeText : '', attemptedBody: parseAttempt(draft.attemptedBody, question) };
  } catch { return emptyDesignDraft(); }
}

export interface DesignChoiceDrafts {
  readonly persistent: boolean;
  read(scope: string, question: DesignChoiceQuestion): DesignChoiceDraft;
  write(scope: string, draft: DesignChoiceDraft): void;
  remove(scope: string): void;
}

/** Provider-owned memory survives dock/full remounts; guarded persistence survives a reload. */
export function createDesignChoiceDrafts(storage: DraftStorage | null, userId: string): DesignChoiceDrafts {
  const memory = new Map<string, string>();
  const key = (scope: string) => `partroute.assistant.design.v1.${encodeURIComponent(userId)}.${scope}`;
  let persistent = !!storage;
  return {
    get persistent() { return persistent; },
    read(scope, question) {
      let raw = memory.get(scope) ?? null;
      if (!memory.has(scope)) {
        try { raw = storage?.getItem(key(scope)) ?? null; } catch { persistent = false; }
      }
      return parseDesignDraft(raw, question);
    },
    write(scope, draft) {
      const raw = JSON.stringify(draft);
      memory.set(scope, raw);
      try { storage?.setItem(key(scope), raw); } catch { persistent = false; }
    },
    remove(scope) {
      // A tombstone prevents failed storage cleanup from reviving an acknowledged request on remount.
      const empty = JSON.stringify(emptyDesignDraft());
      if (memory.get(scope) === empty) return;
      memory.set(scope, empty);
      try { storage?.removeItem(key(scope)); } catch { persistent = false; }
    },
  };
}
