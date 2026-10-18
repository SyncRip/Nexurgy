import { describe, expect, it } from 'vitest';
import { DICTS, translate } from '../../i18n/core';
import type { TFunction } from '../../i18n/I18nProvider';
import { draftingCommandPreview } from './draftingPlanPreview';

describe('drawing plan human review', () => {
  it('uses executable geometry values and distinguishes label-only changes in both languages', () => {
    for (const lang of ['zh', 'en'] as const) {
      const t: TFunction = (key, vars) => translate(DICTS[lang], key, vars);
      const geometry = draftingCommandPreview(t, { op: 'update_entity', id: 'E-hole', set: { radius: 5 } }).join(' ');
      const label = draftingCommandPreview(t, { op: 'edit_dimension', id: 'E-dim', text_override: '4×Ø10' }).join(' ');
      expect(geometry).toContain(lang === 'zh' ? '半径 → 5' : 'Radius → 5');
      expect(label).toContain(lang === 'zh' ? '显示文字 → 4×Ø10' : 'Displayed text → 4×Ø10');
      expect(geometry).toContain('E-hole');
      expect(label).toContain('E-dim');
      expect(draftingCommandPreview(t, { op: 'edit_dimension', id: 'E-dim', text_override: null })[0]).toContain(t('core.plan.measuredLabel'));
    }
  });
});
