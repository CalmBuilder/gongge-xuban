import { createContext, useContext, type ReactNode } from 'react';

import type { EnterpriseContext as EnterpriseContextValue } from './auth';

const EnterpriseContext = createContext<EnterpriseContextValue | null>(null);

export function EnterpriseContextProvider({
  value,
  children,
}: {
  value: EnterpriseContextValue;
  children: ReactNode;
}) {
  return <EnterpriseContext.Provider value={value}>{children}</EnterpriseContext.Provider>;
}

export function useEnterpriseContext(): EnterpriseContextValue {
  const context = useContext(EnterpriseContext);
  if (!context) {
    throw new Error('EnterpriseContextProvider is required');
  }
  return context;
}
