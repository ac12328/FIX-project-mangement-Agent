"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";

import { LegacyDraftDetailView } from "@/components/draft-detail-legacy";
import { DraftWorkspaceView } from "@/components/draft-workspace/draft-workspace";
import { ErrorPanel, LoadingPanel } from "@/components/state-panel";
import { getRuntimeCapabilities } from "@/lib/api";

export default function DraftDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [workspaceEnabled, setWorkspaceEnabled] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getRuntimeCapabilities()
      .then((capabilities) => {
        if (!cancelled) setWorkspaceEnabled(capabilities.draft_workspace.enabled);
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : "Unable to load draft workspace configuration");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return (
      <div className="mx-auto max-w-4xl py-8">
        <ErrorPanel description={error} onRetry={() => window.location.reload()} />
      </div>
    );
  }

  if (workspaceEnabled === null) {
    return <LoadingPanel label="Preparing draft workspace" />;
  }

  return workspaceEnabled
    ? <DraftWorkspaceView draftId={id} />
    : <LegacyDraftDetailView draftId={id} />;
}
