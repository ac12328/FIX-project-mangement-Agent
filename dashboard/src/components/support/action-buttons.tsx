"use client";

import { Button } from "@/components/ui/button";
import { Check, FolderPlus, Mail, RotateCcw, StickyNote } from "lucide-react";

interface ActionButtonsProps {
  status: string;
  ball: string;
  loading: boolean;
  nextAction?: string;
  onAction: (actionType: string, params?: Record<string, string>) => void;
  onAddNote: () => void;
}

export default function ActionButtons({
  status, ball, loading, nextAction, onAction, onAddNote,
}: ActionButtonsProps) {
  const terminal = status === "resolved" || status === "closed";

  return (
    <div className="rounded-xl border bg-card shadow-sm">
      <div className="border-b px-5 py-3 text-sm font-semibold text-foreground">Actions</div>
      <div className="flex flex-col gap-2 p-5">
        {!terminal && nextAction === "close_stale" ? (
          <Button className="justify-start gap-2.5" disabled={loading} onClick={() => onAction("close_stale")}>
            <Check className="size-4" /> Close as stale
          </Button>
        ) : null}
        {!terminal && nextAction !== "close_stale" ? (
          <Button className="justify-start gap-2.5 bg-success hover:bg-success/90 text-white" disabled={loading} onClick={() => onAction("resolve")}>
            <Check className="size-4" /> Mark done
          </Button>
        ) : null}
        {terminal ? (
          <Button variant="outline" className="justify-start gap-2.5" disabled={loading} onClick={() => onAction("reopen")}>
            <RotateCcw className="size-4" /> Reopen
          </Button>
        ) : null}
        {ball === "External" && !terminal ? (
          <Button className="justify-start gap-2.5" disabled={loading} onClick={() => onAction("send_followup")}>
            <Mail className="size-4" /> Follow up
          </Button>
        ) : null}
        {!terminal ? (
          <Button variant="outline" className="justify-start gap-2.5" disabled={loading} onClick={() => onAction("not_an_issue")}>
            <FolderPlus className="size-4" /> Not a desk issue
          </Button>
        ) : null}
        <Button variant="outline" className="justify-start gap-2.5" onClick={onAddNote}>
          <StickyNote className="size-4" /> Add internal note
        </Button>
      </div>
    </div>
  );
}
