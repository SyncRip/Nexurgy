import { useEffect, useState } from 'react';

const visible = () => typeof document === 'undefined' || document.visibilityState !== 'hidden';

export function useDocumentVisible(): boolean {
  const [value, setValue] = useState(visible);
  useEffect(() => {
    const onVisibility = () => setValue(visible());
    document.addEventListener('visibilitychange', onVisibility);
    onVisibility();
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, []);
  return value;
}
