import { afterEach, describe, expect, it, vi } from 'vitest';
import { answerAiDesignChoice, type DesignChoiceQuestion, type DesignChoiceReceipt, type DesignQuestion } from '../../api/assistant';
import { setTokenProvider } from '../../api/client';
import { collectDesignAnswers, createDesignChoiceDrafts, DESIGN_TEXT_LIMIT, DESIGN_VALUE_LIMIT, designChoiceScope, designReceiptMatches, emptyDesignDraft, parseDesignDraft, prepareDesignChoice, type DesignChoiceDraft } from './designChoices';

const question = (id: string, input_kind: DesignQuestion['input_kind'] = 'number', extra: Partial<DesignQuestion> = {}): DesignQuestion => ({
  id, key: id, label: id, description: '', input_kind, unit: null, minimum: null, maximum: null,
  options: [], recommended: null, allow_custom: true, required: true, ...extra,
});
const choices: DesignChoiceQuestion = {
  kind: 'design_choice', title: 'Choose design details', description: '',
  design_ref: { schema_version: 1, plan_id: 'plan', version_id: 'version-1', version: 1, sha256: 'a'.repeat(64) },
  question_set_id: 'question-set-1', can_answer: true, allowed_actions: ['customize', 'delegate_remaining', 'accept_recommended'], last_response: null,
  questions: [question('width', 'number', { unit: 'mm', minimum: 0, recommended: { kind: 'value', value: 40 } }), question('coat', 'boolean'), question('material', 'string', { options: [{ id: 'steel', label: 'Steel', value: 'S235' }], recommended: { kind: 'option', option_id: 'steel' } })],
};
const input = (text: string) => ({ kind: 'value' as const, text });
const draft = (patch: Partial<DesignChoiceDraft> = {}): DesignChoiceDraft => ({ ...emptyDesignDraft(), ...patch });

afterEach(() => { vi.unstubAllGlobals(); setTokenProvider(() => null); });

describe('dynamic design answers', () => {
  it('allows partial answers, preserves zero and false, and never submits untouched recommendations', () => {
    const result = collectDesignAnswers(choices.questions, { width: input('0'), coat: input('false') });
    expect(result).toEqual({ answers: [{ question_id: 'width', kind: 'value', value: 0 }, { question_id: 'coat', kind: 'value', value: false }], errors: {} });
    expect(collectDesignAnswers(choices.questions, { width: input('  '), coat: input('') }).answers).toEqual([]);
    expect(prepareDesignChoice(choices, draft(), 'accept_recommended', () => 'new')?.answers).toEqual([]);
    expect(prepareDesignChoice(choices, draft(), 'delegate_remaining', () => 'new')?.answers).toEqual([]);
    expect(prepareDesignChoice(choices, draft(), 'customize', () => 'new')).toBeNull();
  });

  it.each(['NaN', 'Infinity', '1e300', '12 mm', '12 + 3', '2×20', '1,2'])('refuses ambiguous or invalid scalar %s without silently extracting a number', (text) => {
    expect(collectDesignAnswers(choices.questions, { width: input(text) }).errors.width).toBe('number');
  });

  it('preserves signed decimals and checks bounds without coercing negative numbers positive', () => {
    const fields = [question('offset', 'number', { minimum: -2, maximum: 2 })];
    expect(collectDesignAnswers(fields, { offset: input(' -.5 ') }).answers[0]).toMatchObject({ value: -0.5 });
    expect(collectDesignAnswers(fields, { offset: input('-3') }).errors.offset).toBe('minimum');
    expect(collectDesignAnswers(fields, { offset: input('3') }).errors.offset).toBe('maximum');
    expect(collectDesignAnswers(fields, { offset: input('9'.repeat(400)) }).errors.offset).toBe('number');
  });

  it('sends option ids rather than option labels and validates custom-entry authority and booleans', () => {
    expect(collectDesignAnswers(choices.questions, { material: { kind: 'option', option_id: 'steel' } }).answers).toEqual([{ question_id: 'material', kind: 'option', option_id: 'steel' }]);
    expect(collectDesignAnswers(choices.questions, { material: { kind: 'option', option_id: 'S235' } }).errors.material).toBe('option');
    expect(collectDesignAnswers([question('fixed', 'string', { allow_custom: false })], { fixed: input('custom') }).errors.fixed).toBe('custom');
    expect(collectDesignAnswers(choices.questions, { coat: input('0') }).errors.coat).toBe('boolean');
  });

  it('keeps explicit edits and raw free text with recommendations or delegation', () => {
    const edited = draft({ values: { width: input('32') }, freeText: '  Make it lighter.\nChoose the rest.  ' });
    for (const action of ['accept_recommended', 'delegate_remaining', 'customize'] as const) {
      const body = prepareDesignChoice(choices, edited, action, () => 'request');
      expect(body).toMatchObject({ action, answers: [{ question_id: 'width', kind: 'value', value: 32 }], free_text: edited.freeText, design_ref: choices.design_ref });
    }
    expect(prepareDesignChoice(choices, draft({ freeText: 'Choose a lighter design' }), 'customize', () => 'request')?.answers).toEqual([]);
    expect(prepareDesignChoice(choices, draft({ freeText: 'x'.repeat(DESIGN_TEXT_LIMIT + 1) }), 'delegate_remaining', () => 'request')).toBeNull();
    expect(collectDesignAnswers(choices.questions, { material: input('x'.repeat(DESIGN_VALUE_LIMIT + 1)) }).errors.material).toBe('length');
  });

  it('keeps authority on the server projection, including retry attempts', () => {
    const attemptedBody = prepareDesignChoice(choices, draft(), 'delegate_remaining', () => 'request')!;
    expect(prepareDesignChoice({ ...choices, can_answer: false }, draft({ attemptedBody }), 'delegate_remaining', () => 'new')).toBeNull();
    expect(prepareDesignChoice({ ...choices, allowed_actions: ['customize'] }, draft(), 'accept_recommended', () => 'new')).toBeNull();
    expect(prepareDesignChoice({ ...choices, question_set_id: 'other' }, draft({ attemptedBody }), 'delegate_remaining', () => 'new')).toBeNull();
  });

  it('treats server question ids as data even when they match object property names', () => {
    expect(collectDesignAnswers([question('constructor')], {}).answers).toEqual([]);
    expect(collectDesignAnswers([question('__proto__')], { ['__proto__']: input('invalid') }).errors.__proto__).toBe('number');
  });
});

describe('durable design choice attempts', () => {
  const storage = () => {
    const map = new Map<string, string>();
    return { getItem: (key: string) => map.get(key) ?? null, setItem: (key: string, value: string) => { map.set(key, value); }, removeItem: (key: string) => { map.delete(key); } };
  };

  it('replays the exact body and id after provider remount or page reload', () => {
    const disk = storage(), scope = designChoiceScope('conversation', 'op', choices);
    const first = createDesignChoiceDrafts(disk, 'alice');
    const original = draft({ values: { width: input('32') }, freeText: 'leave the rest to the Agent' });
    const attemptedBody = prepareDesignChoice(choices, original, 'delegate_remaining', () => 'request')!;
    first.write(scope, { ...original, attemptedBody });
    const restored = createDesignChoiceDrafts(disk, 'alice').read(scope, choices);
    expect(restored).toEqual({ ...original, attemptedBody });
    const newId = vi.fn(() => 'wrong');
    expect(prepareDesignChoice(choices, restored, 'delegate_remaining', newId)).toEqual(attemptedBody);
    expect(newId).not.toHaveBeenCalled();
    expect(createDesignChoiceDrafts(disk, 'bob').read(scope, choices)).toEqual(emptyDesignDraft());
    expect(first.read(designChoiceScope('different-conversation', 'op', choices), choices)).toEqual(emptyDesignDraft());
    expect(first.read(designChoiceScope('conversation', 'different-op', choices), choices)).toEqual(emptyDesignDraft());
    const next = { ...choices, design_ref: { ...choices.design_ref, version_id: 'version-2', version: 2 } };
    expect(first.read(designChoiceScope('conversation', 'op', next), next)).toEqual(emptyDesignDraft());
  });

  it('does not restore malformed, foreign or duplicate-answer attempts', () => {
    const attemptedBody = prepareDesignChoice(choices, draft({ values: { width: input('10') } }), 'customize', () => 'request')!;
    expect(parseDesignDraft('{broken', choices)).toEqual(emptyDesignDraft());
    for (const bad of [
      { ...attemptedBody, design_ref: { ...choices.design_ref, sha256: 'b'.repeat(64) } },
      { ...attemptedBody, question_set_id: 'other' },
      { ...attemptedBody, answers: [...attemptedBody.answers, ...attemptedBody.answers] },
      { ...attemptedBody, free_text: null },
      { ...attemptedBody, answers: [{ question_id: 'coat', kind: 'value', value: 'false' }] },
    ]) expect(parseDesignDraft(JSON.stringify(draft({ attemptedBody: bad as typeof attemptedBody })), choices).attemptedBody).toBeNull();
  });

  it('uses answered identity to reconcile a receipt before or after the new plan exists', () => {
    const attemptedBody = prepareDesignChoice(choices, draft(), 'delegate_remaining', () => 'request')!;
    const receipt: DesignChoiceReceipt = { client_request_id: 'request', question_set_id: choices.question_set_id, answered_design_ref: choices.design_ref, design_ref: null, action: 'delegate_remaining', resume_operation_id: 'wake' };
    expect(designReceiptMatches(attemptedBody, receipt)).toBe(true);
    expect(designReceiptMatches(attemptedBody, { ...receipt, design_ref: { ...choices.design_ref, version_id: 'new', version: 2 } })).toBe(true);
    expect(designReceiptMatches(attemptedBody, { ...receipt, answered_design_ref: { ...choices.design_ref, version: 2 } })).toBe(false);
    expect(designReceiptMatches(attemptedBody, { ...receipt, client_request_id: 'another' })).toBe(false);
    expect(designReceiptMatches(attemptedBody, { ...receipt, question_set_id: 'another' })).toBe(false);
  });

  it('retains drafts across remounts when storage fails and clears acknowledged bodies', () => {
    const disk = { getItem: () => { throw Error('disabled'); }, setItem: () => { throw Error('disabled'); }, removeItem: () => { throw Error('disabled'); } };
    const drafts = createDesignChoiceDrafts(disk, 'alice');
    const original = draft({ values: { coat: input('false') } });
    drafts.write('scope', original);
    expect(drafts.persistent).toBe(false);
    expect(drafts.read('scope', choices)).toEqual(original);
    drafts.remove('scope');
    expect(drafts.read('scope', choices)).toEqual(emptyDesignDraft());
  });

  it('posts the exact bound design body to its additive endpoint', async () => {
    const result = { id: 'op', status: 'succeeded' };
    const fetcher = vi.fn(async () => new Response(JSON.stringify(result), { headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetcher);
    const body = prepareDesignChoice(choices, draft({ freeText: '  Keep this text.  ' }), 'delegate_remaining', () => 'request')!;
    await expect(answerAiDesignChoice('op', body)).resolves.toEqual(result);
    const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/ai\/operations\/op\/design-choice$/);
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual(body);
  });
});
