import { createElement as h } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AiOperation, DesignChoiceQuestion, DesignPlanSummary as DesignPlan } from '../../api/assistant';
import { I18nProvider } from '../../i18n/I18nProvider';
import { DesignChoiceCard } from './DesignChoiceCard';
import { DesignPlanSummary } from './DesignPlanSummary';
import { createDesignChoiceDrafts, designChoiceScope, emptyDesignDraft, prepareDesignChoice } from './designChoices';
import { designChoiceQuestion } from './designChoiceFixtures.test-support';

const session = { designDrafts: createDesignChoiceDrafts(null, 'user'), store: { answerDesign: vi.fn(), refresh: vi.fn() } };
vi.mock('./AssistantProvider', () => ({ useAssistantSession: () => session }));

const question: DesignChoiceQuestion = { ...designChoiceQuestion, questions: designChoiceQuestion.questions.slice(0, 1) };
const operation = (patch: Partial<AiOperation> = {}): AiOperation => ({ id: 'op', conversation_id: 'conversation', status: 'waiting_input', result: null, ...patch }) as AiOperation;
const render = (q = question, disabled = false, op = operation()) => renderToStaticMarkup(h(I18nProvider, null, h(DesignChoiceCard, { op, question: q, disabled })));

beforeEach(() => { session.designDrafts = createDesignChoiceDrafts(null, 'user'); vi.clearAllMocks(); });

describe('design choice presentation and authority', () => {
  it('renders server-defined questions, recommendations and free text without preselecting a user answer', () => {
    const html = render();
    expect(html).toContain('Mounting width (mm)');
    expect(html).toContain('Medium width');
    expect(html).toContain('Recommended');
    expect(html).not.toContain('checked=""');
    expect(html).toContain('Additional preferences (optional)');
    expect(html).toMatch(/maxlength="4000"/i);
    expect(html).toContain('Use recommendations');
    expect(html).toContain('Choose the parameters for me');
    expect(session.store.answerDesign).not.toHaveBeenCalled();
  });

  it('renders only server-allowed actions and makes closed or archived questions read-only', () => {
    const restricted = render({ ...question, allowed_actions: ['delegate_remaining'] });
    expect(restricted).toContain('Choose the parameters for me');
    expect(restricted).not.toContain('Use recommendations');
    expect(restricted).not.toContain('Submit my choices');
    for (const html of [render({ ...question, can_answer: false }), render(question, true), render(question, false, operation({ status: 'cancelled' }))]) {
      expect(html).toContain('These choices are read-only');
      expect(html).toContain('<fieldset class="as-design-field" disabled=""');
      expect(html).not.toContain('as-design-actions');
      expect(html).toContain('Medium width');
    }
  });

  it('retains submitted edits during an unknown outcome and exposes only an exact retry', () => {
    const scope = designChoiceScope('conversation', 'op', question);
    const draft = { ...emptyDesignDraft(), values: { width: { kind: 'value' as const, text: '42' } }, freeText: 'Make the remaining choices' };
    session.designDrafts.write(scope, { ...draft, attemptedBody: prepareDesignChoice(question, draft, 'delegate_remaining', () => 'request') });
    const html = render();
    expect(html).toContain('value="42"');
    expect(html).toContain('Make the remaining choices');
    expect(html).toContain('Retry submitted choices');
    expect(html).not.toContain('Use recommendations');
    expect(html).toContain('<fieldset class="as-design-field" disabled=""');
    const nextQuestion = { ...question, question_set_id: 'set2', design_ref: { ...question.design_ref, version_id: 'v2', version: 2 } };
    expect(render(nextQuestion)).not.toContain('value="42"');
  });
});

describe('saved design history', () => {
  it('retains actual origins and prior options without claiming CAD acceptance', () => {
    const plan: DesignPlan = {
      ref: question.design_ref, title: 'Fixture design', summary: 'A reusable fixture with two supports.', status: 'adopted',
      structure: { root: 'fixture', definitions: 3, part_definitions: 2, assembly_definitions: 1, instances: 3 },
      decisions: [{ id: 'width', key: 'width', label: 'Mounting width', value: 80, unit: 'mm', reason: 'Fits the envelope', origin: 'ai_design' },
        { id: 'material', key: 'material', label: 'Material', value: 'Steel', unit: null, reason: 'User requirement', origin: 'user_input' }],
      questions: question.questions, last_response: null,
    };
    const html = renderToStaticMarkup(h(I18nProvider, null, h(DesignPlanSummary, { plan })));
    expect(html).toContain('AI design');
    expect(html).toContain('Your input');
    expect(html).toContain('80 mm');
    expect(html).toContain('Planned structure: 1 assembly definitions, 2 part definitions, 3 instance records.');
    expect(html).toContain('Saved design questions (1)');
    expect(html).toContain('Medium width');
    expect(html).not.toMatch(/accepted|passed|confirmed|<button|<input/);
  });
});
