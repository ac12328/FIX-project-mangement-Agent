"use client";

import { useState } from "react";
import {
  ArchiveRestore,
  Bot,
  Check,
  Clock3,
  GitCompareArrows,
  Sparkles,
} from "lucide-react";

import { formatDate } from "@/components/draft-workspace/draft-workspace-shared";
import { WorkspaceDialog } from "@/components/draft-workspace/workspace-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { TabsContent } from "@/components/ui/tabs";
import {
  type AIProposal,
  type CheckpointComparison,
  type DraftWorkspace,
  compareDraftCheckpoint,
} from "@/lib/draft-workspace-api";

interface WorkspaceSidePanelHistoryProps {
  workspace: DraftWorkspace;
  busy: boolean;
  readOnly: boolean;
  onRestore: (version: number) => Promise<void>;
  onApplyProposal: (proposal: AIProposal, selectedChanges?: string[]) => Promise<void>;
  onRejectProposal: (proposal: AIProposal) => Promise<void>;
  onError: (message: string) => void;
}

function proposalFields(proposal: AIProposal): string[] {
  return ["subject", "body_html", "body_text"].filter(
    (key) => typeof proposal.proposal[key] === "string",
  );
}

export function WorkspaceSidePanelHistory({
  workspace,
  busy,
  readOnly,
  onRestore,
  onApplyProposal,
  onRejectProposal,
  onError,
}: WorkspaceSidePanelHistoryProps) {
  const [comparison, setComparison] = useState<CheckpointComparison | null>(null);
  const [comparisonVersion, setComparisonVersion] = useState<number | null>(null);

  const compare = async (version: number) => {
    if (comparisonVersion !== null || busy) return;

    setComparisonVersion(version);
    try {
      setComparison(await compareDraftCheckpoint(workspace.draft.draft_id, version));
    } catch (error) {
      onError(error instanceof Error ? error.message : "Could not compare versions.");
    } finally {
      setComparisonVersion(null);
    }
  };

  return (
    <>
      <TabsContent value="history" className="max-h-[calc(100vh-10rem)] overflow-y-auto p-3">
        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
            Checkpoints
          </h3>
          <div className="space-y-2">
            {workspace.history.checkpoints.length ? (
              workspace.history.checkpoints.map((checkpoint) => (
                <article key={checkpoint.checkpoint_id} className="rounded-xl border p-3">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <p className="text-sm font-semibold">Version {checkpoint.version}</p>
                      <p className="text-xs text-muted-foreground">
                        {checkpoint.reason.replaceAll("_", " ")} · {formatDate(checkpoint.created_at)}
                      </p>
                    </div>
                    <Badge variant="outline">r{checkpoint.revision}</Badge>
                  </div>
                  <div className="mt-2 flex gap-1">
                    <Button
                      type="button"
                      size="xs"
                      variant="ghost"
                      disabled={comparisonVersion !== null || busy}
                      onClick={() => void compare(checkpoint.version)}
                    >
                      <GitCompareArrows className="size-3" />
                      {comparisonVersion === checkpoint.version ? "Comparing…" : "Compare"}
                    </Button>
                    <Button
                      type="button"
                      size="xs"
                      variant="ghost"
                      disabled={readOnly || busy || comparisonVersion !== null}
                      onClick={() => void onRestore(checkpoint.version)}
                    >
                      <ArchiveRestore className="size-3" /> Restore
                    </Button>
                  </div>
                </article>
              ))
            ) : (
              <p className="rounded-xl border border-dashed p-5 text-center text-xs text-muted-foreground">
                No checkpoints are available.
              </p>
            )}
          </div>
        </section>

        <section className="mt-5">
          <h3 className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
            <Sparkles className="size-3.5" /> Revision-bound proposals
          </h3>
          {workspace.ai_proposals.length ? (
            <div className="space-y-2">
              {workspace.ai_proposals.map((proposal) => {
                const fields = proposalFields(proposal);
                return (
                  <article key={proposal.proposal_id} className="rounded-xl border p-3">
                    <div className="flex items-center justify-between gap-2">
                      <p className="text-sm font-semibold capitalize">
                        <Bot className="mr-1 inline size-4 text-primary" />
                        {proposal.action.replaceAll("_", " ")}
                      </p>
                      <Badge variant={proposal.status === "open" ? "outline" : "secondary"}>
                        {proposal.status}
                      </Badge>
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Based on revision {proposal.base_revision} · {formatDate(proposal.created_at)}
                    </p>
                    {fields.map((field) => (
                      <div key={field} className="mt-2 rounded-lg bg-muted/35 p-2">
                        <p className="text-xs font-semibold uppercase text-muted-foreground">
                          {field.replaceAll("_", " ")}
                        </p>
                        <p className="mt-1 line-clamp-5 whitespace-pre-wrap text-xs">
                          {String(proposal.proposal[field])}
                        </p>
                      </div>
                    ))}
                    {proposal.status === "open" && (
                      <div className="mt-2 flex gap-1">
                        <Button
                          type="button"
                          size="xs"
                          disabled={readOnly || busy}
                          onClick={() => void onApplyProposal(proposal, fields)}
                        >
                          <Check className="size-3" /> Apply
                        </Button>
                        <Button
                          type="button"
                          size="xs"
                          variant="ghost"
                          disabled={busy}
                          onClick={() => void onRejectProposal(proposal)}
                        >
                          Reject
                        </Button>
                      </div>
                    )}
                  </article>
                );
              })}
            </div>
          ) : (
            <p className="rounded-xl border border-dashed p-5 text-center text-xs leading-5 text-muted-foreground">
              No stored proposals. Generated content is only applied through a visible, revision-bound proposal.
            </p>
          )}
        </section>

        <section className="mt-5">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
            Audit trail
          </h3>
          {workspace.history.audit.length ? (
            <ol className="space-y-2">
              {workspace.history.audit.slice(0, 30).map((event) => (
                <li key={event.event_id} className="flex gap-2 text-xs">
                  <Clock3 className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                  <div>
                    <p className="font-medium">{event.event_type.replaceAll("_", " ")}</p>
                    <p className="text-muted-foreground">
                      {event.actor_user_id} · {formatDate(event.created_at)}
                    </p>
                  </div>
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-xs text-muted-foreground">No audit events are available.</p>
          )}
        </section>
      </TabsContent>

      <WorkspaceDialog
        open={Boolean(comparison)}
        title={comparison ? `Compare version ${comparison.version}` : "Compare version"}
        description="Saved checkpoint compared with the current working snapshot."
        onClose={() => setComparison(null)}
        footer={
          <Button type="button" variant="outline" onClick={() => setComparison(null)}>
            Close
          </Button>
        }
      >
        {comparison && (
          <div className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-xl border p-3">
                <p className="text-xs font-semibold uppercase text-muted-foreground">Saved subject</p>
                <p className="mt-1 text-sm">{comparison.subject_before || "(No subject)"}</p>
              </div>
              <div className="rounded-xl border p-3">
                <p className="text-xs font-semibold uppercase text-muted-foreground">Current subject</p>
                <p className="mt-1 text-sm">{comparison.subject_after || "(No subject)"}</p>
              </div>
            </div>
            <pre className="max-h-[50vh] overflow-auto rounded-xl bg-slate-950 p-4 text-xs leading-5 text-slate-100">
              {comparison.body_diff.length
                ? comparison.body_diff.join("\n")
                : "No body differences."}
            </pre>
          </div>
        )}
      </WorkspaceDialog>
    </>
  );
}
