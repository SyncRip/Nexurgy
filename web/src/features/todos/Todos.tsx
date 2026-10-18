import './todos.css';
import { Bell, ArrowRight, RefreshCw } from 'lucide-react';
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { listTodos, todoHref, type TodoItem, type TodosOut } from '../../api/todos';
import { useUser } from '../../auth/AuthProvider';
import { Chip } from '../../components/ui/Badges';
import { Popover } from '../../components/ui/Popover';
import { EmptyState, ErrorNotice, SkeletonRows } from '../../components/ui/States';
import { useResource } from '../../hooks/useResource';
import { useI18n } from '../../i18n/I18nProvider';
import { onApprovalsChanged } from '../approvals/meta';
import { useAssistant } from '../assistant/AssistantProvider';
import { onTodosChanged } from './events';

interface TodosState { data?: TodosOut; items: TodoItem[]; error: unknown; loading: boolean; moreBusy: boolean; reload: () => void; more: () => void }
const Context = createContext<TodosState | null>(null);

export function TodosProvider({ children }: { children: ReactNode }) {
  const { id } = useUser();
  const { onOperationEvent } = useAssistant();
  const location = useLocation();
  const res = useResource((signal) => listTodos({}, { signal }), [id]);
  const [pages, setPages] = useState<TodoItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [moreBusy, setMoreBusy] = useState(false);
  const [moreError, setMoreError] = useState<unknown>(null);
  const generation = useRef(0);
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const reload = useCallback(() => {
    generation.current += 1;
    setCursor(null);
    res.reload();
  }, [res.reload]);
  useEffect(() => { generation.current += 1; setPages([]); setCursor(res.data?.next_cursor ?? null); setMoreError(null); }, [res.data]);
  useEffect(reload, [location.pathname, reload]);
  useEffect(() => {
    const load = () => { if (!document.hidden) reload(); };
    const timer = window.setInterval(load, 15_000);
    const off = onApprovalsChanged(load);
    const op = onOperationEvent(load);
    const offTodos = onTodosChanged(load);
    document.addEventListener('visibilitychange', load);
    return () => { clearInterval(timer); off(); op(); offTodos(); document.removeEventListener('visibilitychange', load); };
  }, [reload, onOperationEvent]);
  const more = async () => {
    if (!cursor || moreBusy) return;
    const gen = generation.current;
    setMoreBusy(true); setMoreError(null);
    try {
      const next = await listTodos({ cursor });
      if (active.current && gen === generation.current) { setPages((old) => [...old, ...next.items]); setCursor(next.next_cursor); }
    } catch (e) { if (active.current && gen === generation.current) setMoreError(e); }
    finally { if (active.current) setMoreBusy(false); }
  };
  const items = [...(res.data?.items ?? []), ...pages];
  return <Context.Provider value={{ data: res.data ? { ...res.data, next_cursor: cursor } : undefined, items, error: moreError ?? res.error, loading: res.loading, reload, moreBusy, more: () => void more() }}>{children}</Context.Provider>;
}

export function useTodos() { const value = useContext(Context); if (!value) throw new Error('TodosProvider is required'); return value; }

export function TodoList({ compact = false, onOpen }: { compact?: boolean; onOpen?: () => void }) {
  const { t, fmt } = useI18n();
  const state = useTodos();
  return <div className="stack-sm">
    <div className="row"><strong>{state.data ? t('core.todo.total', { n: state.data.total }) : t('core.todo.title')}</strong><span className="grow" /><button className="btn ghost icon sm" aria-label={t('status.refresh')} disabled={state.loading} onClick={state.reload}><RefreshCw className={state.loading ? 'spin-icon' : ''} /></button></div>
    <ErrorNotice error={state.error} onRetry={state.reload} />
    {!state.data && !state.error ? <SkeletonRows rows={3} /> : state.data && !state.items.length ? <EmptyState compact title={t('core.todo.empty')} body={t('core.todo.emptyBody')} /> : <ul className="wb-todo">{(compact ? state.items.slice(0, 8) : state.items).map((item) => {
      const href = todoHref(item);
      return <li key={item.key}>{href ? <Link className="wb-todo-row" to={href} onClick={onOpen}><span className="wb-todo-main"><span className="strong">{item.title}</span><span className="small faint">{item.project.code} · {item.object.label}{item.version?.label ? ` · ${item.version.label}` : ''}</span><span className="small">{item.reason.summary}</span><span className="small faint">{fmt.relative(item.created_at)}</span></span><span><Chip tone="pending">{t(`core.todo.${item.source}`)}</Chip><ArrowRight aria-label={t('core.todo.open')} /></span></Link> : <p>{item.title} · {t('core.focus.missing')}</p>}</li>;
    })}</ul>}
    {!compact && state.data?.next_cursor && <button className="btn sm" disabled={state.moreBusy} onClick={state.more}>{t(state.moreBusy ? 'common.loading' : 'core.todo.more')}</button>}
    {compact && <Link to="/home" onClick={onOpen}>{t('core.todo.title')} <ArrowRight className="inline-ico" /></Link>}
  </div>;
}

export function TodoCard({ description }: { description?: string }) { const { t } = useI18n(); return <section className="panel wb-card"><header className="wb-todo-heading"><h2>{t('core.todo.title')}</h2>{description && <p>{description}</p>}</header><TodoList /></section>; }
export function TodoBell() {
  const { t } = useI18n();
  const state = useTodos();
  return <Popover label={t('core.todo.title')} trigger={<><Bell />{state.data && state.data.total > 0 && <span className="badge">{state.data.total}</span>}</>} triggerClassName="btn ghost icon" align="end">{(close) => <div className="todo-popover"><TodoList compact onOpen={close} /></div>}</Popover>;
}
