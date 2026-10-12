"use client";

import { useAuth } from "@/lib/auth-context";

type PermissionGuardProps = {
  permission: string;
  children: React.ReactNode;
  fallback?: React.ReactNode;
};

export function PermissionGuard({ permission, children, fallback = null }: PermissionGuardProps) {
  const { hasPermission } = useAuth();
  return hasPermission(permission) ? children : fallback;
}
