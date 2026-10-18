// Generation supplement E (layered delivery, BHD1 §8.7): the sub-assembly packages of a layered drawing step and a
// delivery's per-level summary (assembly / sub-assemblies / parts, each with its STEP and DWG status). Shown in the
// chain values (run card, node drawer, AI workspace card); everything comes from the recorded value.

import type { DeliveryLevels, LevelFile, LevelObject, SubassemblyPackage } from '../../api/orchestration';
import { Chip, type Tone } from '../../components/ui/Badges';
import { hasKey } from '../../i18n/core';
import type { MessageKey } from '../../i18n/en';
import { useI18n, type TFunction } from '../../i18n/I18nProvider';
import { DELIVERY_LEVELS, levelLines, objectLabel, protocolCell, representationCell, subPackageState } from './levelsModel';
import { LevelStateSummary } from './StateBadge';
import './orchestration.css';

const TONE: Record<string, Tone> = { generated: 'ok', complete: 'ok', partial: 'warn', missing: 'warn', failed: 'err', protocol_unmet: 'err' };
const REP_TONE: Record<string, Tone> = { exact: 'ok', nominal: 'measure', simplified: 'warn', placeholder: 'warn' };

function tx(t: TFunction, key: string, fallback: string, vars?: Record<string, string | number>): string {
  return hasKey(key) ? t(key as MessageKey, vars) : fallback;
}

/** "Sub-assembly drawings (3)": each sub-assembly package with its number, revision and state (a failed one with its code). */
export function SubassemblyPackages({ items }: { items: SubassemblyPackage[] }) {
  const { t } = useI18n();
  if (!items.length) return null;
  return (
    <div className="lv-subs">
      <span className="small strong">{t('lv.subs.title', { n: items.length })}</span>
      <ul>
        {items.map((s) => {
          const state = subPackageState(s);
          return (
            <li key={s.assembly_revision_id}>
              <span className="mono">{objectLabel(s)}</span>
              {s.depth > 1 && <span className="faint"> · L{s.depth}</span>}{' '}
              <Chip tone={TONE[state] ?? 'warn'}>{state === 'failed' ? t('lv.subs.failed') : state === 'not_started' ? t('lv.subs.notStarted') : tx(t, `orch.completeness.${state}`, state)}</Chip>
              {s.error && <code className="error-code">{s.error.code}</code>}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function FileState({ f }: { f: LevelFile }) {
  const { t } = useI18n();
  return (
    <span title={f.message ?? undefined}>
      <Chip tone={TONE[f.status] ?? 'warn'}>{tx(t, `lv.status.${f.status}`, f.status)}</Chip>
      {f.status !== 'generated' && f.code && <code className="error-code">{f.code}</code>}
    </span>
  );
}

/** Generation reliability M: the protocol read from the STEP's header against the required one (a labelled alternative
 * when the required one could not be written). */
function ProtocolState({ f, required }: { f: LevelFile; required: string }) {
  const { t } = useI18n();
  const c = protocolCell(f, required);
  if (!c.protocol && !c.alternative) return <span className="faint">—</span>;
  return (
    <span title={c.reexportedFrom ? t('lv.protocol.reexported', { from: c.reexportedFrom }) : undefined}>
      <Chip tone={c.met ? 'ok' : 'err'}>{c.alternative ? t('lv.protocol.alternative', { protocol: c.alternative }) : c.protocol}</Chip>
    </span>
  );
}

/** Representation level and, when the build record lists them, the requirements its build did not create. */
function RepresentationState({ o }: { o: LevelObject }) {
  const { t } = useI18n();
  const r = representationCell(o);
  if (!r.level) return <span className="faint">—</span>;
  return (
    <span title={r.notCreated.join('\n') || undefined}>
      <Chip tone={REP_TONE[r.level] ?? 'measure'}>{tx(t, `lv.rep.${r.level}`, r.level)}</Chip>
      {r.notCreated.length > 0 && <span className="faint"> · {t('lv.notCreated', { n: r.notCreated.length })}</span>}
    </span>
  );
}

/** One line per level ("3 sub-assemblies: STEP 3/3 · DWG 2/3"); outside the compact card, every object with its two files. */
export function DeliveryLevelsView({ levels, compact }: { levels: DeliveryLevels; compact?: boolean }) {
  const { t } = useI18n();
  const lines = levelLines(levels);
  const required = levels.step_protocol_required ?? 'AP203';
  return (
    <div className="lv">
      <span className="row wrap">
        <span className="small strong">{t('lv.title')}</span>
        {/* generation reliability N: the objects counted per state */}
        <LevelStateSummary levels={levels} />
      </span>
      <ul className="lv-lines">
        {lines.map((l) => (
          <li key={l.level} className={l.complete ? undefined : 'lv-gap'}>
            {t('lv.line', { level: t(`lv.level.${l.level}` as MessageKey), objects: l.objects, step: l.step, dwg: l.dwg })}
          </li>
        ))}
      </ul>
      {!compact && (
        <details>
          <summary className="small">{t('lv.show')}</summary>
          <table className="lv-table">
            <thead>
              <tr>
                <th>{t('lv.col.object')}</th>
                <th>{t('lv.col.step')}</th>
                <th>{t('lv.col.protocol', { required })}</th>
                <th>{t('lv.col.dwg')}</th>
                <th>{t('lv.col.representation')}</th>
              </tr>
            </thead>
            <tbody>
              {DELIVERY_LEVELS.flatMap((level) =>
                levels[level].map((o, i) => (
                  <tr key={`${level}-${o.key ?? i}`}>
                    <td>
                      <span className="faint">{t(`lv.level.${level}` as MessageKey)}</span> <span className="mono">{objectLabel(o)}</span>
                      {o.name && o.number && <span className="faint"> {o.name}</span>}
                      {o.coverage && <span className="faint"> · {t('lv.notBuilt', { stage: tx(t, `cov.stage.${o.stage}`, o.stage ?? '') })}</span>}
                    </td>
                    <td>
                      <FileState f={o.step} />
                    </td>
                    <td>
                      <ProtocolState f={o.step} required={required} />
                    </td>
                    <td>
                      <FileState f={o.dwg} />
                    </td>
                    <td>
                      <RepresentationState o={o} />
                    </td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}
