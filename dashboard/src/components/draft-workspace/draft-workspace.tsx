"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { DraftWorkspaceLayout } from "@/components/draft-workspace/draft-workspace-layout";
import {
  conflictDetail, deliveryLocked, formFromWorkspace, leaseDetail,
  type SaveState, type WorkingForm,
} from "@/components/draft-workspace/draft-workspace-shared";
import { useDraftDeliveryActions } from "@/components/draft-workspace/use-draft-delivery-actions";
import { getMemory, getProject } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import {
  type AIProposal, type DraftWorkspace, type WorkspaceDraft, type WorkspaceEnvelope,
  type WorkspaceLeaseSummary, acquireDraftLease, applyAIProposal,
  createDraftCheckpoint, getDraftWorkspace, patchDraftWorkspace, rejectAIProposal,
  releaseDraftLease, renewDraftLease, restoreDraftCheckpoint, takeOverDraftLease,
} from "@/lib/draft-workspace-api";
import type { MemoryItem, ProjectDetail } from "@/lib/types";

interface DraftWorkspaceProps { draftId: string; }

export function DraftWorkspaceView({ draftId }: DraftWorkspaceProps) {
  const { session } = useAuth();
  const [workspace, setWorkspace] = useState<DraftWorkspace | null>(null);
  const [form, setForm] = useState<WorkingForm | null>(null);
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [memory, setMemory] = useState<MemoryItem[]>([]);
  const [loadingError, setLoadingError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>("saved");
  const [leaseToken, setLeaseToken] = useState<string | null>(null);
  const [leaseConflict, setLeaseConflict] = useState<WorkspaceLeaseSummary | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [showBcc, setShowBcc] = useState(false);
  const [conflict, setConflict] = useState<WorkspaceDraft | null>(null);

  const workspaceRef = useRef<DraftWorkspace | null>(null);
  const formRef = useRef<WorkingForm | null>(null);
  const dirtyRef = useRef(false);
  const saveStateRef = useRef<SaveState>("saved");
  const leaseTokenRef = useRef<string | null>(null);
  const editSerialRef = useRef(0);
  const savePromiseRef = useRef<Promise<DraftWorkspace> | null>(null);
  const flushAutosaveRef = useRef<() => Promise<DraftWorkspace>>(async () => { throw new Error("Autosave is not ready."); });
  const idleCheckpointRef = useRef<number | null>(null);

  const installWorkspace = useCallback((next: DraftWorkspace, resetForm = true) => {
    workspaceRef.current = next;
    setWorkspace(next);
    if (resetForm) {
      const nextForm = formFromWorkspace(next);
      formRef.current = nextForm;
      setForm(nextForm);
      dirtyRef.current = false;
      setDirty(false);
      saveStateRef.current = "saved";
      setSaveState("saved");
    }
    if (next.draft.envelope.bcc.length) setShowBcc(true);
  }, []);

  const acquireLease = useCallback(async () => {
    const current = workspaceRef.current;
    if (!current || deliveryLocked(current.draft)) return;
    try {
      const lease = await acquireDraftLease(draftId);
      leaseTokenRef.current = lease.lease_token;
      setLeaseToken(lease.lease_token);
      setLeaseConflict(null);
    } catch (error) {
      const active = leaseDetail(error);
      if (active) setLeaseConflict(active);
      else setActionError(error instanceof Error ? error.message : "Could not acquire edit access.");
    }
  }, [draftId]);

  const load = useCallback(async () => {
    setLoadingError(null);
    try {
      const next = await getDraftWorkspace(draftId);
      installWorkspace(next);
      if (next.draft.project_id) {
        void Promise.all([getProject(next.draft.project_id), getMemory(next.draft.project_id)])
          .then(([projectResult, memoryResult]) => { setProject(projectResult); setMemory(memoryResult); })
          .catch(() => undefined);
      }
      if (!deliveryLocked(next.draft)) await acquireLease();
    } catch (error) {
      setLoadingError(error instanceof Error ? error.message : "Could not load draft workspace.");
    }
  }, [acquireLease, draftId, installWorkspace]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    leaseTokenRef.current = leaseToken;
    if (!leaseToken) return;
    const timer = window.setInterval(() => {
      void renewDraftLease(draftId, leaseToken).then(() => setLeaseConflict(null)).catch((error) => {
        leaseTokenRef.current = null; setLeaseToken(null); setLeaseConflict(leaseDetail(error));
        setActionError("Your edit lease expired. Reacquire or take over the draft before editing.");
      });
    }, 45_000);
    return () => window.clearInterval(timer);
  }, [draftId, leaseToken]);
  useEffect(() => () => {
    const token = leaseTokenRef.current;
    if (token) void releaseDraftLease(draftId, token, true).catch(() => undefined);
    if (idleCheckpointRef.current) clearTimeout(idleCheckpointRef.current);
  }, [draftId]);
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => {
      if (!dirtyRef.current && saveState !== "saving" && saveState !== "conflict") return;
      event.preventDefault(); event.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [saveState]);

  const updateForm = useCallback((updater: (current: WorkingForm) => WorkingForm) => {
    const current = formRef.current;
    if (!current) return;
    const next = updater(current);
    formRef.current = next;
    setForm(next);
    editSerialRef.current += 1;
    dirtyRef.current = true;
    setDirty(true);
    saveStateRef.current = "unsaved";
    setSaveState("unsaved");
    setNotice(null);

    if (idleCheckpointRef.current) window.clearTimeout(idleCheckpointRef.current);
    idleCheckpointRef.current = window.setTimeout(() => {
      void flushAutosaveRef.current()
        .then(() => createDraftCheckpoint(draftId, "idle_checkpoint"))
        .then(() => getDraftWorkspace(draftId))
        .then((nextWorkspace) => installWorkspace(nextWorkspace, !dirtyRef.current))
        .catch((error) => setActionError(error instanceof Error ? error.message : "Could not create an idle checkpoint."));
    }, 120_000);
  }, [draftId, installWorkspace]);

  const saveWorkingCopy = useCallback(async (): Promise<DraftWorkspace> => {
    const currentWorkspace = workspaceRef.current;
    const currentForm = formRef.current;
    if (!currentWorkspace || !currentForm) throw new Error("Draft workspace is not ready.");
    if (!dirtyRef.current) return currentWorkspace;
    if (saveStateRef.current === "conflict") throw new Error("Resolve the revision conflict before saving.");
    if (!leaseTokenRef.current) throw new Error("Acquire the edit lease before saving.");

    if (savePromiseRef.current) {
      await savePromiseRef.current;
      if (!dirtyRef.current) return workspaceRef.current ?? currentWorkspace;
      return flushAutosaveRef.current();
    }

    const serial = editSerialRef.current;
    const envelopePatch: Partial<WorkspaceEnvelope> = {
      to: currentForm.envelope.to,
      cc: currentForm.envelope.cc,
      bcc: currentForm.envelope.bcc,
    };
    if (currentForm.envelope.sender_identity_id) {
      envelopePatch.sender_identity_id = currentForm.envelope.sender_identity_id;
    }
    saveStateRef.current = "saving";
    setSaveState("saving");
    const request = patchDraftWorkspace(
      draftId,
      currentWorkspace.draft.revision.number,
      {
        content: { subject: currentForm.subject, body_html: currentForm.bodyHtml },
        envelope: envelopePatch,
      },
      leaseTokenRef.current,
    );
    savePromiseRef.current = request;

    try {
      const next = await request;
      const unchangedDuringSave = editSerialRef.current === serial;
      installWorkspace(next, unchangedDuringSave);
      if (!unchangedDuringSave) {
        dirtyRef.current = true;
        setDirty(true);
        saveStateRef.current = "unsaved";
        setSaveState("unsaved");
      }
      setConflict(null);
      return next;
    } catch (error) {
      const latest = conflictDetail(error);
      if (latest) {
        setConflict(latest);
        saveStateRef.current = "conflict";
        setSaveState("conflict");
      } else {
        saveStateRef.current = "error";
        setSaveState("error");
      }
      throw error;
    } finally {
      savePromiseRef.current = null;
    }
  }, [draftId, installWorkspace]);

  useEffect(() => { flushAutosaveRef.current = saveWorkingCopy; }, [saveWorkingCopy]);
  useEffect(() => {
    if (!dirty || !leaseToken || saveState === "conflict") return;
    const timer = window.setTimeout(() => {
      void saveWorkingCopy().catch((error) => {
        setActionError(error instanceof Error ? error.message : "Autosave failed.");
      });
    }, 1_500);
    return () => window.clearTimeout(timer);
  }, [dirty, form, leaseToken, saveState, saveWorkingCopy]);

  const refreshWorkspace = useCallback(async () => {
    if (dirtyRef.current) await flushAutosaveRef.current();
    const next = await getDraftWorkspace(draftId);
    installWorkspace(next, true);
  }, [draftId, installWorkspace]);

  const dropLease = useCallback(async () => {
    const token = leaseTokenRef.current;
    leaseTokenRef.current = null;
    setLeaseToken(null);
    if (token) await releaseDraftLease(draftId, token).catch(() => undefined);
  }, [draftId]);
  const delivery = useDraftDeliveryActions({
    draftId,
    sessionUserId: session?.user_id,
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
  });

  const handleRestore = async (version: number) => {
    if (!window.confirm(`Restore version ${version} as a new working revision? Current history will be preserved.`)) return;
    setBusyAction("restore"); setActionError(null);
    try {
      const saved = await flushAutosaveRef.current();
      const next = await restoreDraftCheckpoint(draftId, version, saved.draft.revision.number, leaseTokenRef.current);
      installWorkspace(next);
      setNotice(`Version ${version} restored as a new revision. Any approval is now stale.`);
    } catch (error) { setActionError(error instanceof Error ? error.message : "Could not restore the checkpoint."); }
    finally { setBusyAction(null); }
  };

  const handleApplyProposal = async (proposal: AIProposal, selectedChanges?: string[]) => {
    setBusyAction("ai-apply"); setActionError(null);
    try {
      if (dirtyRef.current) await flushAutosaveRef.current();
      const next = await applyAIProposal(draftId, proposal.proposal_id, selectedChanges, leaseTokenRef.current);
      installWorkspace(next);
      setNotice("AI proposal applied as a checkpointed revision.");
    } catch (error) { setActionError(error instanceof Error ? error.message : "Could not apply the proposal."); }
    finally { setBusyAction(null); }
  };

  const handleRejectProposal = async (proposal: AIProposal) => {
    setBusyAction("ai-reject"); setActionError(null);
    try {
      await rejectAIProposal(draftId, proposal.proposal_id);
      await refreshWorkspace();
      setNotice("AI proposal rejected without changing the draft.");
    } catch (error) { setActionError(error instanceof Error ? error.message : "Could not reject the proposal."); }
    finally { setBusyAction(null); }
  };

  const handleTakeover = async () => {
    if (!window.confirm("Take over editing? The current editor will lose write access and this action will be audited.")) return;
    setBusyAction("takeover"); setActionError(null);
    try {
      const lease = await takeOverDraftLease(draftId);
      leaseTokenRef.current = lease.lease_token;
      setLeaseToken(lease.lease_token);
      setLeaseConflict(null);
      setNotice("Edit lease taken over. The prior holder can still comment but cannot save.");
      await refreshWorkspace();
    } catch (error) { setActionError(error instanceof Error ? error.message : "Could not take over editing."); }
    finally { setBusyAction(null); }
  };

  const reloadConflict = async () => {
    setBusyAction("conflict-reload");
    try { const next = await getDraftWorkspace(draftId); installWorkspace(next); setConflict(null); }
    catch (error) { setActionError(error instanceof Error ? error.message : "Could not reload the latest revision."); }
    finally { setBusyAction(null); }
  };

  const reapplyConflict = async () => {
    const local = formRef.current;
    if (!local) return;
    setBusyAction("conflict-reapply"); setActionError(null);
    try {
      const latest = await getDraftWorkspace(draftId);
      installWorkspace(latest, false);
      formRef.current = local;
      setForm(local);
      dirtyRef.current = true;
      setDirty(true);
      saveStateRef.current = "unsaved";
      setSaveState("unsaved");
      setConflict(null);
      await saveWorkingCopy();
      setNotice("Your local version was explicitly reapplied on top of the latest revision.");
    } catch (error) { setActionError(error instanceof Error ? error.message : "Could not reapply your version."); }
    finally { setBusyAction(null); }
  };


  const handleSave = async () => {
    setActionError(null);
    try {
      await flushAutosaveRef.current();
      setNotice("Working copy saved.");
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not save the working copy.");
    }
  };

  const handleCheckpoint = async () => {
    setBusyAction("checkpoint");
    setActionError(null);
    try {
      await flushAutosaveRef.current();
      await createDraftCheckpoint(draftId, "manual_checkpoint");
      await refreshWorkspace();
      setNotice("Checkpoint created.");
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not create a checkpoint.");
    } finally {
      setBusyAction(null);
    }
  };
  return (
    <DraftWorkspaceLayout
      workspace={workspace}
      form={form}
      project={project}
      memory={memory}
      sessionUserId={session?.user_id}
      loadingError={loadingError}
      actionError={actionError}
      notice={notice}
      dirty={dirty}
      saveState={saveState}
      leaseToken={leaseToken}
      leaseConflict={leaseConflict}
      busyAction={busyAction}
      showBcc={showBcc}
      preview={delivery.preview}
      previewOpen={delivery.previewOpen}
      retryMode={delivery.retryMode}
      conflict={conflict}
      reviewerUserId={delivery.reviewerUserId}
      rescheduleValue={delivery.rescheduleValue}
      undoSeconds={delivery.undoSeconds}
      flushAutosave={flushAutosaveRef.current}
      onWorkspaceChange={installWorkspace}
      onLoad={load}
      onAcquireLease={acquireLease}
      onUpdateForm={updateForm}
      onSave={handleSave}
      onCheckpoint={handleCheckpoint}
      onTakeover={handleTakeover}
      onConfirmNew={delivery.handleConfirmNew}
      onConfirmReply={delivery.handleConfirmReply}
      onRestore={handleRestore}
      onApplyProposal={handleApplyProposal}
      onRejectProposal={handleRejectProposal}
      onRefreshWorkspace={refreshWorkspace}
      onRefreshPreflight={delivery.refreshPreflight}
      onOpenFinalPreview={delivery.openFinalPreview}
      onOpenRetryPreview={delivery.openRetryPreview}
      onSendNow={delivery.handleSendNow}
      onSendLater={delivery.handleSendLater}
      onUndo={delivery.handleUndo}
      onCancelSchedule={delivery.handleCancelSchedule}
      onReschedule={delivery.handleReschedule}
      onRequestReview={delivery.handleRequestReview}
      onReviewAction={delivery.handleReviewAction}
      onSetPreviewOpen={delivery.setPreviewOpen}
      onSetActionError={setActionError}
      onSetShowBcc={setShowBcc}
      onSetReviewerUserId={delivery.setReviewerUserId}
      onSetRescheduleValue={delivery.setRescheduleValue}
      onReloadConflict={reloadConflict}
      onReapplyConflict={reapplyConflict}
    />
  );
}
