// Section choice of a drawing package (cad/drawing.py PackageConfig.section):
// automatic, no section, or an explicit cutting plane XZ (at Y = offset) /
// YZ (at X = offset) in model mm. Used by the Drawings tab generate form and
// the workflow editor's `drawing` node.

import { useEffect, useId, useState } from 'react';
import type { DrawingSection } from '../../api/types';
import { useI18n } from '../../i18n/I18nProvider';
import { parseOffset, sectionMode } from './status';
import './cad.css';

export function SectionPicker({
  value,
  onChange,
  disabled,
  size = 'sm',
  id,
  describedBy,
}: {
  value: DrawingSection | null | undefined;
  onChange: (v: DrawingSection) => void;
  disabled?: boolean;
  size?: 'sm' | 'md';
  id?: string;
  describedBy?: string;
}) {
  const { t } = useI18n();
  const ownId = useId();
  const selId = id ?? ownId;
  const planeId = useId();
  const offId = useId();
  const mode = sectionMode(value);
  const plane = typeof value === 'object' && value ? value.plane : 'XZ';
  const offset = typeof value === 'object' && value ? value.offset : null;
  const [text, setText] = useState(offset === null ? '' : String(offset));
  useEffect(() => {
    if (offset !== null && parseOffset(text) !== offset) setText(String(offset));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [offset]);
  const cls = size === 'sm' ? 'sm' : '';
  const invalid = mode === 'plane' && parseOffset(text) === null;
  return (
    <div className="cad-section">
      <select
        id={selId}
        className={`select ${cls}`}
        value={mode}
        disabled={disabled}
        aria-describedby={describedBy}
        onChange={(e) => {
          const m = e.target.value;
          if (m === 'auto' || m === 'none') onChange(m);
          else onChange({ plane, offset: parseOffset(text) ?? 0 });
        }}
      >
        <option value="auto">{t('cad.dw.section.auto')}</option>
        <option value="none">{t('cad.dw.section.none')}</option>
        <option value="plane">{t('cad.dw.section.plane')}</option>
      </select>
      {mode === 'plane' && (
        <div className="cad-section-plane">
          <label className="small" htmlFor={planeId}>
            <span className="faint">{t('cad.dw.section.planeLabel')}</span>
            <select id={planeId} className={`select ${cls}`} value={plane} disabled={disabled} onChange={(e) => onChange({ plane: e.target.value === 'YZ' ? 'YZ' : 'XZ', offset: parseOffset(text) ?? 0 })}>
              <option value="XZ">{t('cad.dw.section.XZ')}</option>
              <option value="YZ">{t('cad.dw.section.YZ')}</option>
            </select>
          </label>
          <label className="small" htmlFor={offId}>
            <span className="faint">{t('cad.dw.section.offset')} (mm)</span>
            <input
              id={offId}
              className={`input mono ${cls} ${invalid ? 'is-invalid' : ''}`}
              type="text"
              inputMode="decimal"
              value={text}
              disabled={disabled}
              aria-invalid={invalid || undefined}
              onChange={(e) => {
                setText(e.target.value);
                const v = parseOffset(e.target.value);
                if (v !== null) onChange({ plane, offset: v });
              }}
            />
          </label>
          {invalid && <span className="field-error small">{t('cad.dw.section.offsetRequired')}</span>}
        </div>
      )}
    </div>
  );
}
