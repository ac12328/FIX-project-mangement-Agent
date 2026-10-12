"use client";

import type { ReactNode } from "react";

import { useAuth } from "@/lib/auth-context";

export function PermissionBoundary({
  permission,
  children,
  fallback = null,
}: {
  permission: string;
  children: ReactNode;
  fallback?: ReactNode;
}) {
  const { hasPermission } = useAuth();
  return hasPermission(permission) ? <>{children}</> : <>{fallback}</>;
}
