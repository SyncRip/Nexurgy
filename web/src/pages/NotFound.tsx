import { Link } from 'react-router-dom';
import { EmptyState } from '../components/ui/States';
import { useI18n } from '../i18n/I18nProvider';

export default function NotFound({ home = '/projects' }: { home?: string }) {
  const { t } = useI18n();
  return (
    <div className="page">
      <EmptyState
        title={t('error.pageNotFound')}
        body={t('error.pageNotFoundBody')}
        action={
          <Link to={home} className="btn">
            {t('nav.projects')}
          </Link>
        }
      />
    </div>
  );
}
