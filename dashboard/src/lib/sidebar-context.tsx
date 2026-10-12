"use client";

import { createContext, useCallback, useContext, useEffect, useState, useSyncExternalStore } from "react";

const STORAGE_KEY = "agentfixb.sidebar.collapsed";
const STORAGE_EVENT = "agentfixb-sidebar-change";

interface SidebarContextValue {
  mobileOpen: boolean;
  desktopCollapsed: boolean;
  openMobile: () => void;
  closeMobile: () => void;
  toggleMobile: () => void;
  toggleDesktop: () => void;
  isOpen: boolean;
  open: () => void;
  close: () => void;
  toggle: () => void;
}

const SidebarContext = createContext<SidebarContextValue | null>(null);

function subscribeDesktopPreference(onStoreChange: () => void) {
  window.addEventListener("storage", onStoreChange);
  window.addEventListener(STORAGE_EVENT, onStoreChange);
  return () => {
    window.removeEventListener("storage", onStoreChange);
    window.removeEventListener(STORAGE_EVENT, onStoreChange);
  };
}

function getDesktopPreference() {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

export function SidebarProvider({ children }: { children: React.ReactNode }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const desktopCollapsed = useSyncExternalStore(subscribeDesktopPreference, getDesktopPreference, () => false);

  const openMobile = useCallback(() => setMobileOpen(true), []);
  const closeMobile = useCallback(() => setMobileOpen(false), []);
  const toggleMobile = useCallback(() => setMobileOpen((open) => !open), []);
  const toggleDesktop = useCallback(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, String(!getDesktopPreference()));
    } catch {
      // Storage can be unavailable in hardened browser contexts.
    }
    window.dispatchEvent(new Event(STORAGE_EVENT));
  }, []);

  useEffect(() => {
    if (!mobileOpen) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [mobileOpen]);

  return (
    <SidebarContext.Provider
      value={{
        mobileOpen,
        desktopCollapsed,
        openMobile,
        closeMobile,
        toggleMobile,
        toggleDesktop,
        isOpen: mobileOpen,
        open: openMobile,
        close: closeMobile,
        toggle: toggleMobile,
      }}
    >
      {children}
    </SidebarContext.Provider>
  );
}

export function useSidebar() {
  const context = useContext(SidebarContext);
  if (!context) throw new Error("useSidebar must be used within SidebarProvider");
  return context;
}
