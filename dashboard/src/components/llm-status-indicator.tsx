"use client";

import { useEffect, useState } from "react";
import { getLLMStatus, type LLMHealthResponse } from "@/lib/api";
import { cn } from "@/lib/utils";

type DotStatus = "ok" | "down" | "unknown" | "loading";

function Dot({ status }: { status: DotStatus }) {
  return (
    <span
      className={cn(
        "inline-block h-2 w-2 rounded-full",
        status === "ok" && "bg-success",
        status === "down" && "bg-danger",
        status === "unknown" && "bg-muted-foreground/30",
        status === "loading" && "bg-muted-foreground/30 animate-pulse",
      )}
    />
  );
}

function dotStatus(reachable: boolean, latency: number | null, loading: boolean): DotStatus {
  if (loading) return "loading";
  if (latency === null && !reachable) return "unknown";
  return reachable ? "ok" : "down";
}

export function LLMStatusIndicator() {
  const [data, setData] = useState<LLMHealthResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    const check = () => {
      getLLMStatus()
        .then((r) => { if (mounted) { setData(r); setLoading(false); } })
        .catch(() => { if (mounted) setLoading(false); });
    };
    check();
    const interval = setInterval(check, 30_000);
    return () => { mounted = false; clearInterval(interval); };
  }, []);

  const pStatus = dotStatus(data?.primary.reachable ?? false, data?.primary.latency_ms ?? null, loading);
  const bStatus = dotStatus(data?.backup.reachable ?? false, data?.backup.latency_ms ?? null, loading);

  return (
    <div className="flex items-center gap-2 px-2 py-1 text-xs text-muted-foreground/50">
      <div className="flex items-center gap-1">
        <Dot status={pStatus} />
        {data?.primary.latency_ms != null && (
          <span>{data.primary.latency_ms}ms</span>
        )}
      </div>
      <div className="flex items-center gap-1">
        <Dot status={bStatus} />
        {data?.backup.latency_ms != null && (
          <span>{data.backup.latency_ms}ms</span>
        )}
      </div>
    </div>
  );
}
