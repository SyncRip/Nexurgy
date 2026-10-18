// Draft input policy (contract §2.2): standard mode with input rules, or
// exploratory (all rules relaxed; provenance labels still propagate). Saved on the
// draft (PUT /workflows/{id} {policy}); it takes effect from the next published version.

import { Save } from 'lucide-react';
import { useState } from 'react';
import { updateWorkflow } from '../../api/endpoints';
import { APPROVAL_STAGES, type ApprovalStage, type TemplatePolicy, type Workflow } from '../../api/types';
import { ErrorNotice } from '../../components/ui/States';
import { useToast } from '../../components/ui/Toast';
import { useI18n } from '../../i18n/I18nProvider';
import { stageLabel } from '../workflow/labels';
import { normalizePolicy, RULE_FLAGS, TPL_ERRORS } from './common';

export function PolicyEditor({ workflow, readOnly, onSaved }: { workflow: Workflow; readOnly: boolean; onSaved: (w: Workflow) => void }) {
  const { t } = useI18n();
  const toast = useToast();
  const saved = normalizePolicy(workflow.policy);
  const [policy, setPolicy] = useState<TemplatePolicy>(saved);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const dirty = JSON.stringify(policy) !== JSON.stringify(saved);
  const exploratory = policy.mode === 'exploratory';
  const setRule = <K extends keyof TemplatePolicy['input_rules']>(k: K, v: TemplatePolicy['input_rules'][K]) => setPolicy((p) => ({ ...p, input_rules: { ...p.input_rules, [k]: v } }));

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const w = await updateWorkflow(workflow.id, { policy, expected_version: workflow.version });
      onSaved(w);
      setPolicy(normalizePolicy(w.policy ?? policy));
      toast(t('tpl.policy.savedToast'));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="stack-sm"
      onSubmit={(e) => {
        e.preventDefault();
        if (!readOnly && dirty && !busy) void save();
      }}
    >
      <fieldset className="tpl-radio-row" disabled={readOnly || busy}>
        <legend className="tk-label">{t('tpl.policy.mode')}</legend>
        <label className="check">
          <input type="radio" name="tpl-mode" checked={!exploratory} onChange={() => setPolicy((p) => ({ ...p, mode: 'standard' }))} />
          <span>
            <span className="strong">{t('tpl.policy.standard')}</span> <span className="faint small">{t('tpl.policy.standardHint')}</span>
          </span>
        </label>
        <label className="check">
          <input type="radio" name="tpl-mode" checked={exploratory} onChange={() => setPolicy((p) => ({ ...p, mode: 'exploratory' }))} />
          <span>
            <span className="strong">{t('tpl.policy.exploratory')}</span> <span className="faint small">{t('tpl.policy.exploratoryHint')}</span>
          </span>
        </label>
      </fieldset>
      <fieldset className="tpl-rules" disabled={readOnly || busy || exploratory}>
        <legend className="tk-label">{t('tpl.policy.rules')}</legend>
        {RULE_FLAGS.map((f) => (
          <label key={f} className="check">
            <input type="checkbox" checked={policy.input_rules[f]} onChange={(e) => setRule(f, e.target.checked)} />
            <span>{t(`tpl.rule.${f}` as 'tpl.rule.allow_estimated')}</span>
          </label>
        ))}
        <label className="field">
          <span>{t('tpl.rule.require_approved_stage')}</span>
          <select className="select" value={policy.input_rules.require_approved_stage ?? ''} onChange={(e) => setRule('require_approved_stage', (e.target.value || null) as ApprovalStage | null)}>
            <option value="">{t('tpl.rule.noApproval')}</option>
            {APPROVAL_STAGES.map((s) => (
              <option key={s} value={s}>
                {t('tpl.rule.approvedAt', { stage: stageLabel(t, s) })}
              </option>
            ))}
          </select>
        </label>
      </fieldset>
      <p className="faint small">{t('tpl.policy.appliesNext')}</p>
      {!readOnly && (
        <div className="row">
          <button type="submit" className="btn sm primary" disabled={!dirty || busy}>
            <Save /> {busy ? t('common.saving') : t('common.save')}
          </button>
          {dirty && (
            <button type="button" className="btn sm ghost" onClick={() => setPolicy(saved)} disabled={busy}>
              {t('common.cancel')}
            </button>
          )}
        </div>
      )}
      <ErrorNotice error={error} overrides={TPL_ERRORS} />
    </form>
  );
}
