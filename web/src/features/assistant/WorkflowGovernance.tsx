import { createContext,useContext,useState,type ReactNode } from 'react';
interface Governance { autoApply: boolean; setAutoApply(value: boolean): void }
const Context = createContext<Governance | null>(null);
export function WorkflowGovernanceProvider({ userId, children }: { userId: string; children: ReactNode }) {
  const key = `partroute.workflowAi.autoApply.${userId}`;
  const [autoApply, setState] = useState(() => { try { return localStorage.getItem(key) === '1'; } catch { return false; } });
  const setAutoApply = (value: boolean) => { setState(value); try { localStorage.setItem(key, value ? '1' : '0'); } catch { /* session only */ } };
  return <Context.Provider value={{ autoApply, setAutoApply }}>{children}</Context.Provider>;
}
export const useWorkflowAi = () => useContext(Context);
