"use client";

import { useEffect, useState } from "react";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";

import { formatDate } from "@/components/draft-workspace/draft-workspace-shared";
import {
  type DraftWorkspace,
  type PreflightResult,
  approveDraftReview,
  cancelDraftReview,
  cancelScheduledDraft,
  confirmNewEmail,
  confirmReplyThread,
  getDraftPreflight,
  getRetryDraftPreview,
  requestDraftChanges,
  requestDraftReview,
  rescheduleDraft,
  retryDraftSend,
  sendDraftLater,
  sendDraftNow,
  undoDraftSend,
} from "@/lib/draft-workspace-api";

interface DeliveryActionsOptions {
  draftId: string;
  sessionUserId?: string;
  workspace: DraftWorkspace | null;
  workspaceRef: MutableRefObject<DraftWorkspace | null>;
  leaseTokenRef: MutableRefObject<string | null>;
  flushAutosaveRef: MutableRefObject<() => Promise<DraftWorkspace>>;
  installWorkspace: (workspace: DraftWorkspace, resetForm?: boolean) => void;
  load: () => Promise<void>;
  dropLease: () => Promise<void>;
  setBusyAction: Dispatch<SetStateAction<string | null>>;
  setActionError: Dispatch<SetStateAction<string | null>>;
  setNotice: Dispatch<SetStateAction<string | null>>;
}

export function useDraftDeliveryActions({
  draftId,
  sessionUserId,
  workspace,
  workspaceRef,
  leaseTokenRef,
  flushAutosaveRef,
  installWorkspace,
  load,
  dropLease,
  setBusyAction,
  setActionError,
  setNotice,
}: DeliveryActionsOptions) {
  const [preview, setPreview] = useState<PreflightResult | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [retryMode, setRetryMode] = useState(false);
  const [reviewerUserId, setReviewerUserId] = useState("");
  const [rescheduleValue, setRescheduleValue] = useState("");
  const [undoSeconds, setUndoSeconds] = useState(0);

  useEffect(() => {
    const scheduledFor = workspace?.outbox?.scheduled_for;
    if (!scheduledFor) return;
    const date = new Date(scheduledFor);
    if (Number.isNaN(date.valueOf())) return;
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
    setRescheduleValue(local.toISOString().slice(0, 16));
  }, [workspace?.outbox?.scheduled_for]);

  useEffect(() => {
    const undoUntil = workspace?.outbox?.status === "undoable" ? workspace.outbox.undo_until : null;
    if (!undoUntil) {
      setUndoSeconds(0);
      return;
    }
    const tick = () => setUndoSeconds(Math.max(0, Math.ceil((new Date(undoUntil).getTime() - Date.now()) / 1_000)));
    tick();
    const timer = window.setInterval(tick, 500);
    return () => window.clearInterval(timer);
  }, [workspace?.outbox?.status, workspace?.outbox?.undo_until]);

  const handleConfirmNew = async () => {
    setBusyAction("delivery");
    setActionError(null);
    try {
      const saved = await flushAutosaveRef.current();
      const next = await confirmNewEmail(draftId, saved.draft.revision.number, leaseTokenRef.current);
      installWorkspace(next);
      setNotice("Delivery context confirmed as a new email.");
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not confirm a new email.");
    } finally {
      setBusyAction(null);
    }
  };

  const handleConfirmReply = async (emailId: string) => {
    setBusyAction("delivery");
    setActionError(null);
    try {
      const saved = await flushAutosaveRef.current();
      const next = await confirmReplyThread(draftId, saved.draft.revision.number, emailId, leaseTokenRef.current);
      installWorkspace(next);
      setNotice("Reply anchor confirmed and Safe Reply All recipients applied.");
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not confirm the reply thread.");
    } finally {
      setBusyAction(null);
    }
  };

  const refreshPreflight = async (): Promise<PreflightResult | null> => {
    setBusyAction("preflight");
    setActionError(null);
    try {
      const saved = await flushAutosaveRef.current();
      const nextPreflight = await getDraftPreflight(draftId);
      installWorkspace({ ...saved, preflight: nextPreflight }, false);
      return nextPreflight;
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not refresh preflight.");
      return null;
    } finally {
      setBusyAction(null);
    }
  };

  const openFinalPreview = async () => {
    const nextPreflight = await refreshPreflight();
    if (!nextPreflight) return;
    setPreview(nextPreflight);
    setRetryMode(false);
    if (!nextPreflight.ok) {
      setActionError("Mandatory preflight blockers must be resolved before delivery.");
      document.getElementById("preflight")?.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    setPreviewOpen(true);
  };

  const openRetryPreview = async () => {
    const outbox = workspaceRef.current?.outbox;
    if (!outbox) return;
    setBusyAction("retry-preview");
    setActionError(null);
    try {
      setPreview(await getRetryDraftPreview(draftId, outbox.outbox_id));
      setRetryMode(true);
      setPreviewOpen(true);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not load the immutable retry preview.");
    } finally {
      setBusyAction(null);
    }
  };

  const handleSendNow = async (confirmedWarnings: string[]) => {
    if (!preview) return;
    setBusyAction(retryMode ? "retry" : "send");
    setActionError(null);
    try {
      const currentOutbox = workspaceRef.current?.outbox;
      if (retryMode) {
        if (!currentOutbox) throw new Error("Retry outbox is no longer available.");
        await retryDraftSend(draftId, currentOutbox.outbox_id, confirmedWarnings);
        setNotice("Retry queued from the immutable delivery snapshot.");
      } else {
        await sendDraftNow(draftId, preview.revision, preview.snapshot_hash, confirmedWarnings);
        setNotice("Message is in the outbox. You have 30 seconds to undo.");
      }
      setPreviewOpen(false);
      await dropLease();
      await load();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not queue delivery.");
    } finally {
      setBusyAction(null);
    }
  };

  const handleSendLater = async (confirmedWarnings: string[], scheduledFor: string) => {
    if (!preview || retryMode) return;
    setBusyAction("schedule");
    setActionError(null);
    try {
      await sendDraftLater(draftId, preview.revision, preview.snapshot_hash, confirmedWarnings, scheduledFor);
      setPreviewOpen(false);
      setNotice(`Delivery scheduled for ${formatDate(scheduledFor)}.`);
      await dropLease();
      await load();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not schedule delivery.");
    } finally {
      setBusyAction(null);
    }
  };

  const handleUndo = async () => {
    const outbox = workspaceRef.current?.outbox;
    if (!outbox) return;
    setBusyAction("undo");
    setActionError(null);
    try {
      await undoDraftSend(draftId, outbox.outbox_id);
      setNotice("Delivery was undone. The draft is editable again.");
      await load();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not undo delivery.");
    } finally {
      setBusyAction(null);
    }
  };

  const handleCancelSchedule = async () => {
    const outbox = workspaceRef.current?.outbox;
    if (!outbox || !window.confirm("Cancel this scheduled delivery and return the draft to editing?")) return;
    setBusyAction("cancel-schedule");
    setActionError(null);
    try {
      await cancelScheduledDraft(draftId, outbox.outbox_id);
      setNotice("Scheduled delivery cancelled.");
      await load();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not cancel the schedule.");
    } finally {
      setBusyAction(null);
    }
  };

  const handleReschedule = async () => {
    const outbox = workspaceRef.current?.outbox;
    if (!outbox || !rescheduleValue) return;
    const scheduled = new Date(rescheduleValue);
    if (Number.isNaN(scheduled.valueOf()) || scheduled <= new Date()) {
      setActionError("Choose a future delivery time.");
      return;
    }
    setBusyAction("reschedule");
    setActionError(null);
    try {
      await rescheduleDraft(draftId, outbox.outbox_id, scheduled.toISOString());
      setNotice(`Delivery rescheduled for ${formatDate(scheduled.toISOString())}.`);
      await load();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not reschedule delivery.");
    } finally {
      setBusyAction(null);
    }
  };

  const handleRequestReview = async () => {
    if (!reviewerUserId.trim()) {
      setActionError("Enter a reviewer user ID.");
      return;
    }
    if (reviewerUserId.trim() === sessionUserId) {
      setActionError("Reviewer must be another user.");
      return;
    }
    setBusyAction("review-request");
    setActionError(null);
    try {
      await flushAutosaveRef.current();
      await requestDraftReview(draftId, reviewerUserId.trim());
      setNotice("Review requested. Sending and editing are locked until a decision or cancellation.");
      await dropLease();
      await load();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not request review.");
    } finally {
      setBusyAction(null);
    }
  };

  const handleReviewAction = async (action: "approve" | "changes" | "cancel") => {
    setBusyAction(`review-${action}`);
    setActionError(null);
    try {
      if (action === "approve") await approveDraftReview(draftId);
      if (action === "changes") await requestDraftChanges(draftId);
      if (action === "cancel") await cancelDraftReview(draftId);
      setNotice(action === "approve"
        ? "Draft approved for this exact revision."
        : action === "changes"
          ? "Changes requested. The composer is editable again."
          : "Review cancelled.");
      await load();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not update review state.");
    } finally {
      setBusyAction(null);
    }
  };

  return {
    preview,
    previewOpen,
    retryMode,
    reviewerUserId,
    rescheduleValue,
    undoSeconds,
    setPreviewOpen,
    setReviewerUserId,
    setRescheduleValue,
    handleConfirmNew,
    handleConfirmReply,
    refreshPreflight,
    openFinalPreview,
    openRetryPreview,
    handleSendNow,
    handleSendLater,
    handleUndo,
    handleCancelSchedule,
    handleReschedule,
    handleRequestReview,
    handleReviewAction,
  };
}
