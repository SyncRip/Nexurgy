// Choose one concrete asset version from the project catalogue: filter by type,
// search, pick an asset, then pick an exact version. Nothing is pre-selected —
// "latest" is never chosen implicitly (design §6.5).

import { useEffect, useMemo, useState } from 'react';
import { listAssetVersionsOf, listProjectAssets, type AssetSummary, type AssetType, type AssetVersion } from '../../api/pm';
import type { UUID } from '../../api/types';
import { SearchInput } from '../../components/ui/Form';
import { EmptyState, ErrorNotice, SkeletonRows } from '../../components/ui/States';
import { useDebounced } from '../../hooks/keyboard';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { AssetTypeIcon, assetTypeLabel, AvailabilityChip, EngineeringChip, VersionLabels } from './common';

export function AssetVersionPicker({
  projectId,
  types,
  value,
  onChange,
}: {
  projectId: UUID;
  /** Allowed asset types; empty / undefined = all types. */
  types?: readonly AssetType[];
  value: AssetVersion | null;
  onChange: (v: AssetVersion | null) => void;
}) {
  const { t, fmt } = useI18n();
  const allowed = useMemo(() => (types && types.length ? [...types] : null), [types]);
  const [type, setType] = useState<AssetType | ''>(allowed && allowed.length === 1 ? allowed[0] : '');
  const [q, setQ] = useState('');
  const dq = useDebounced(q.trim(), 250);
  const [assetId, setAssetId] = useState<UUID | null>(value?.asset_id ?? null);

  useEffect(() => {
    if (allowed && type && !allowed.includes(type)) setType(allowed.length === 1 ? allowed[0] : '');
  }, [allowed, type]);

  const assets = useResource((signal) => listProjectAssets(projectId, { type: type || undefined, q: dq || undefined }, { signal }), [projectId, type, dq]);
  const list: AssetSummary[] = useMemo(() => (assets.data ?? []).filter((a) => !allowed || allowed.includes(a.type)), [assets.data, allowed]);
  const versions = useResource((signal) => listAssetVersionsOf(assetId!, { signal }), [assetId], { enabled: !!assetId });
  const selectedAsset = list.find((a) => a.id === assetId) ?? null;

  return (
    <div className="tk-picker">
      <div className="toolbar">
        {(!allowed || allowed.length > 1) && (
          <select className="select" value={type} onChange={(e) => setType(e.target.value as AssetType | '')} aria-label={t('tk.picker.type')}>
            <option value="">{allowed ? t('tk.picker.allowedTypes') : t('tk.picker.allTypes')}</option>
            {(allowed ?? (['part_model', 'drawing', 'process_plan', 'quote', 'document', 'intake'] as AssetType[])).map((x) => (
              <option key={x} value={x}>
                {assetTypeLabel(t, x)}
              </option>
            ))}
          </select>
        )}
        <SearchInput value={q} onChange={setQ} placeholder={t('tk.picker.search')} />
      </div>
      {allowed && <p className="faint small">{t('tk.picker.limitedTo', { types: allowed.map((x) => assetTypeLabel(t, x)).join(', ') })}</p>}
      <div className="tk-picker-cols">
        <fieldset className="tk-picker-col">
          <legend className="tk-label">{t('tk.picker.asset')}</legend>
          {assets.error && !assets.data ? (
            <ErrorNotice error={assets.error} onRetry={assets.reload} />
          ) : assets.initial ? (
            <SkeletonRows rows={4} cols={2} />
          ) : !list.length ? (
            <EmptyState compact title={dq || type ? t('common.noMatches') : t('tk.picker.noAssets')} body={dq || type ? undefined : t('tk.picker.noAssetsBody')} />
          ) : (
            <div className={`tk-pick-list ${assets.loading ? 'is-refreshing' : ''}`}>
              {list.map((a) => (
                <label key={a.id} className={`tk-pick-row ${a.id === assetId ? 'selected' : ''}`}>
                  <input
                    type="radio"
                    name="tk-pick-asset"
                    checked={a.id === assetId}
                    onChange={() => {
                      setAssetId(a.id);
                      if (value && value.asset_id !== a.id) onChange(null);
                    }}
                  />
                  <span className="tk-pick-ico" title={assetTypeLabel(t, a.type)}>
                    <AssetTypeIcon type={a.type} />
                  </span>
                  <span className="grow tk-xs">
                    <span className="ellip strong">{a.name}</span>
                    <span className="faint small">
                      {assetTypeLabel(t, a.type)} · {t('tk.picker.versionCount', { n: a.version_count })}
                      {a.latest_version ? ` · ${t('tk.picker.latest', { label: a.latest_version.label })}` : ''}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          )}
        </fieldset>
        <fieldset className="tk-picker-col">
          <legend className="tk-label">{selectedAsset ? t('tk.picker.versionsOf', { name: selectedAsset.name }) : t('tk.picker.version')}</legend>
          {!assetId ? (
            <p className="faint small">{t('tk.picker.chooseAssetFirst')}</p>
          ) : versions.error && !versions.data ? (
            <ErrorNotice error={versions.error} onRetry={versions.reload} />
          ) : versions.initial || versions.loading ? (
            <SkeletonRows rows={3} cols={2} />
          ) : !versions.data?.length ? (
            <p className="faint small">{t('tk.picker.noVersions')}</p>
          ) : (
            <div className="tk-pick-list">
              {versions.data.map((v) => (
                <label key={v.id} className={`tk-pick-row ${value?.id === v.id ? 'selected' : ''}`}>
                  <input type="radio" name="tk-pick-version" checked={value?.id === v.id} onChange={() => onChange(v)} />
                  <span className="mono strong tk-pick-label">{v.label}</span>
                  <span className="grow tk-xs">
                    <span className="tk-labels">
                      <AvailabilityChip availability={v.availability} />
                      <VersionLabels labels={v.labels} />
                      <EngineeringChip engineering={v.engineering} />
                    </span>
                    <span className="faint small" title={fmt.dateTime(v.created_at)}>
                      {t('tk.picker.created', { time: fmt.relative(v.created_at) })}
                      {v.run_id ? ` · ${t('tk.picker.fromRun')}` : ''}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          )}
        </fieldset>
      </div>
    </div>
  );
}
