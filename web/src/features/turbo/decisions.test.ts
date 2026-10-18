import { describe, expect, it } from 'vitest';
import type { AiDecision } from '../../api/orchestration';
import { DICTS, translate } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import { decisionCounts, decisionLine, decisionProblems, decisionState, decisionView, droppedDecisions, droppedLine, groupDecisions } from './decisions';

const zh = (key: MessageKey, vars?: Record<string, string | number>) => translate(DICTS.zh, key, vars);
const en = (key: MessageKey, vars?: Record<string, string | number>) => translate(DICTS.en, key, vars);

describe('AI decision line (generation supplement D-02)', () => {
  it('reads "AI decision: adopt …; reason: …; impact: …" from a stored assumption', () => {
    const v = decisionView({ id: 'A-01', subject: 'D-03.length', field: 'length', adopted: 30, unit: 'mm', basis: 'M8 through 2×12 plates plus nut',
      affected: ['D-03', 'I-10'], author: 'ai_turbo', trigger: 'missing' });
    expect(decisionLine(v, zh)).toBe('AI 判断：采用 30 mm；原因：M8 through 2×12 plates plus nut；影响：D-03、I-10');
    expect(decisionLine(v, en)).toBe('AI decision: adopt 30 mm; reason: M8 through 2×12 plates plus nut; affects: D-03, I-10');
    expect([v.subject, v.trigger, v.state, v.problems]).toEqual(['D-03.length', 'missing', 'adopted', []]);
  });

  it('says what is missing instead of staying silent, and flags empty / generic / placeholder decisions', () => {
    const empty = decisionView({ subject: 'D-02.thickness', adopted: null, basis: '按常规处理' });
    expect(decisionLine(empty, zh)).toBe('AI 判断：采用 （未给出采用值）；原因：按常规处理；影响：未注明');
    expect(empty.problems).toEqual(['no_value', 'generic_basis']);
    expect(empty.trigger).toBeNull();
    expect(decisionProblems({ subject: 'material', adopted: 'TBD', basis: 'not stated in the request' })).toEqual(['no_value']);
    expect(decisionProblems({ subject: '', adopted: 1, basis: '' })).toEqual(['no_subject', 'no_basis']);
    // the server's own list wins
    expect(decisionProblems({ subject: 'x', adopted: null, problems: ['no_basis'] })).toEqual(['no_basis']);
  });

  it('keeps the state a person gave it and never shows a person value as an AI decision', () => {
    expect(decisionState({ subject: 'a', confirmation: { decision: 'confirmed' } })).toBe('confirmed');
    expect(decisionState({ subject: 'a', state: 'unconfirmed' })).toBe('adopted');
    expect(decisionState({ subject: 'a', replaced_by: 'answer to A-01' })).toBe('replaced');
    const person = decisionView({ subject: 'D-01.width', adopted: 40, unit: 'mm', basis: 'answer to Q-02', author: 'person' });
    expect(person.state).toBe('person');
    expect(decisionLine(person, zh)).toBe('人工给定：40 mm；原因：answer to Q-02；影响：未注明');
  });

  it('shows delegated design parameters as AI decisions awaiting human confirmation', () => {
    const design = decisionView({ id: 'A-100', subject: 'D-01.thickness', adopted: 12, unit: 'mm',
      basis: 'Selected for the stated load; strength remains unverified', author: 'ai_design', state: 'unconfirmed' });
    expect(design.author).toBe('ai_design');
    expect(design.state).toBe('adopted');
    expect(decisionLine(design, zh)).toContain('AI 判断：采用 12 mm');
    expect(zh(`tb.assumptions.author.${design.author}`)).toBe('AI · 设计选定');
    expect(en(`tb.assumptions.author.${design.author}`)).toBe('AI · Design');
  });

  it('says which decisions were not used: a stated value stays, a person answer replaced one', () => {
    const merge = [
      { subject: 'D-01.outer_diameter', adopted: 32, unit: 'mm', reason: 'stated_value', stated: 30, stated_unit: 'mm' },
      { subject: 'D-01.colour', adopted: 'red', reason: 'not_open' },
    ];
    const finish = [{ assumption: 'A-01', subject: 'D-01.thickness', adopted: 2, unit: 'mm', reason: 'answered', stated: 4, stated_unit: 'mm' },
      { subject: 'D-01.outer_diameter', adopted: 32, unit: 'mm', reason: 'stated_value', stated: 30, stated_unit: 'mm' }];
    const out = droppedDecisions(merge, finish, null);
    expect(out.map((d) => d.subject)).toEqual(['D-01.outer_diameter', 'D-01.thickness']);
    expect(droppedLine(out[0], zh)).toBe('AI 判断未采用：D-01.outer_diameter 资料已写明 30 mm 且没有冲突，保留资料值（AI 曾提出 32 mm）');
    expect(droppedLine(out[1], zh)).toBe('AI 判断 A-01 已由人的回答替代：D-01.thickness = 4 mm');
  });

  it('groups server decision records by owner and object and counts triggers and problems', () => {
    const d = (over: Partial<AiDecision>): AiDecision => ({ subject: 'D-01.length', field: 'length', trigger: 'missing', adopted: 1, basis: 'peer value of D-02',
      affected: [], sources: [], alternatives: [], author: 'ai_turbo', state: 'adopted', problems: [], ...over });
    const views = [
      d({ owner: { type: 'engineering_spec_version', id: 's', version_no: 3 } }),
      d({ subject: 'D-01.width', trigger: 'conflict', owner: { type: 'engineering_spec_version', id: 's', version_no: 3 }, alternatives: [12, 14] }),
      d({ subject: 'R2.depth', trigger: null, adopted: null, problems: ['no_value'], owner: { type: 'model_revision', id: 'm', rev_no: 2 } }),
    ].map((x, i) => decisionView(x, i));
    expect(groupDecisions(views).map((g) => [g.object, g.items.length, g.problems])).toEqual([['v3 · D-01', 2, 0], ['R2 · R2', 1, 1]]);
    expect(views[1].alternatives).toEqual(['12', '14']);
    expect(decisionCounts(views)).toEqual({ byTrigger: { missing: 1, ambiguous: 0, conflict: 1, unstated: 1 }, adopted: 3, withProblems: 1 });
  });
});
