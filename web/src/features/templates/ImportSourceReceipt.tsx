import { Link } from 'react-router-dom';
import type { ImportSource } from '../../api/types';
import { paths } from '../../app/nav';
import { useI18n } from '../../i18n/I18nProvider';

export function ImportSourceReceipt({ source, canOpen }: { source: ImportSource | null | undefined; canOpen?: boolean }) {
  const { t } = useI18n();
  if (!source) return null;
  const label = t('core.import.receipt', { name: source.name, version: source.version_no });
  return <span className="small" title={source.hash}>{canOpen ? <Link to={`${paths.workflow(source.workflow_id, 'versions')}?version=${encodeURIComponent(source.template_version_id)}`}>{label}</Link> : <span className="faint">{label}</span>}</span>;
}
