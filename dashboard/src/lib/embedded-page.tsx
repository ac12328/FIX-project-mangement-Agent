"use client";

import { createContext, useContext, type ReactNode } from "react";

const EmbeddedPageContext = createContext(false);

export function EmbeddedPage({ children }: { children: ReactNode }) {
  return <EmbeddedPageContext.Provider value={true}>{children}</EmbeddedPageContext.Provider>;
}

export function useEmbeddedPage() {
  return useContext(EmbeddedPageContext);
}
