// What a published version promises: inputs (type, required, approval), outputs
// (asset type, written as a new version of the part's asset), review gates and the
// input policy. Read from GET /template-versions/{vid}.

import { ArrowDownToLine, ArrowUpFromLine, ShieldCheck } from 'lucide-react';
import type { TemplateVersionDetail } from '../../api/types';
import { Chip } from '../../components/ui/Badges';
import { useI18n } from '../../i18n/I18nProvider';
import { assetTypeLabel } from '../tasks/common';
import { nodeDisplayName, nodeTypeLabel, stageLabel } from '../workflow/labels';
import { dynT, inputAssetType, PolicySummary } from './common';
import { GateList } from './RunInputs';

export function VersionContract({ detail }: { detail: TemplateVersionDetail }) {
  const { t } = useI18n();
  const nameOf = (id: string) => {
    const n = detail.graph.nodes.find((x) => x.id === id);
    return n ? nodeDisplayName(t, n) : id;
  };
  const inputs = detail.input_schema?.inputs ?? [];
  const outputs = detail.output_schema?.outputs ?? [];
  return (
    <div className="tpl-contract">
      <section aria-label={t('tpl.contract.inputs')}>
        <h4 className="tk-label">
          <ArrowDownToLine aria-hidden className="inline-ico" /> {t('tpl.contract.inputs')}
        </h4>
        {inputs.length ? (
          <ul className="tpl-io-list">
            {inputs.map((s) => {
              const type = inputAssetType(s);
              return (
                <li key={s.node_id}>
                  <span className="strong">{nameOf(s.node_id)}</span>
                  <span className="faint small">{type ? assetTypeLabel(t, type) : nodeTypeLabel(t, s.node_type)}</span>
                  {s.required ? <span className="tpl-req small">{t('tpl.input.required')}</span> : <span className="faint small">{t('common.optional')}</span>}
                  {s.require_approved_stage && (
                    <Chip tone="measure" icon={<ShieldCheck aria-hidden />}>
                      {t('tpl.input.needsApproval', { stage: stageLabel(t, s.require_approved_stage) })}
                    </Chip>
                  )}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="faint small">{t('tpl.input.none')}</p>
        )}
      </section>
      <section aria-label={t('tpl.contract.outputs')}>
        <h4 className="tk-label">
          <ArrowUpFromLine aria-hidden className="inline-ico" /> {t('tpl.contract.outputs')}
        </h4>
        {outputs.length ? (
          <ul className="tpl-io-list">
            {outputs.map((o) => (
              <li key={`${o.node_id}:${o.port}`}>
                <span className="strong">{nameOf(o.node_id)}</span>
                <span className="faint small">{assetTypeLabel(t, o.asset_type)}</span>
                <span className="faint small">{dynT(t, `tpl.contract.target.${o.target}`, o.target)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="faint small">{t('tpl.contract.noOutputs')}</p>
        )}
      </section>
      <section aria-label={t('tpl.policy.title')}>
        <h4 className="tk-label">{t('tpl.policy.title')}</h4>
        <PolicySummary policy={detail.policy} />
        <GateList graph={detail.graph} />
      </section>
    </div>
  );
}
