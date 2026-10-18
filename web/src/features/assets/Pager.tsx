// Offset pager for server-paged lists: "page N of M · total" with previous /
// next and a short window of page numbers. The caller stores offset in the URL.

import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useI18n } from '../../i18n/I18nProvider';
import { pageInfo } from './urlState';

export function Pager({ total, limit, offset, onOffset, summaryKey = 'as.pager.summary' }: { total: number; limit: number; offset: number; onOffset: (offset: number) => void; summaryKey?: 'as.pager.summary' | 'as.pager.summaryCollections' }) {
  const { t, fmt } = useI18n();
  const { page, pages } = pageInfo(total, limit, offset);
  const go = (p: number) => onOffset(Math.max(0, (Math.min(pages, Math.max(1, p)) - 1) * limit));
  const first = Math.max(1, Math.min(page - 2, pages - 4));
  const nums = Array.from({ length: Math.min(5, pages) }, (_, i) => first + i);
  return (
    <nav className="as-pager" aria-label={t('as.pager.label')}>
      <span className="faint small">{t(summaryKey, { page: fmt.number(page), pages: fmt.number(pages), total: fmt.number(total) })}</span>
      <span className="grow" />
      {pages > 1 && (
        <div className="as-pager-btns">
          <button type="button" className="btn ghost icon sm" onClick={() => go(page - 1)} disabled={page <= 1} aria-label={t('as.pager.prev')}>
            <ChevronLeft />
          </button>
          {nums.map((n) => (
            <button key={n} type="button" className={`btn sm as-pager-num ${n === page ? 'active' : 'ghost'}`} aria-current={n === page ? 'page' : undefined} aria-label={t('as.pager.page', { n })} onClick={() => go(n)}>
              {fmt.number(n)}
            </button>
          ))}
          <button type="button" className="btn ghost icon sm" onClick={() => go(page + 1)} disabled={page >= pages} aria-label={t('as.pager.next')}>
            <ChevronRight />
          </button>
        </div>
      )}
    </nav>
  );
}
