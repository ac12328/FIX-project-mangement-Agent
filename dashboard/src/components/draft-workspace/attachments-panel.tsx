"use client";

import { useId, useRef, useState } from "react";
import { Download, File as FileIcon, Paperclip, Trash2, UploadCloud } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  deleteWorkspaceAttachment,
  type DraftWorkspace,
  uploadWorkspaceAttachment,
  workspaceAttachmentDownloadUrl,
} from "@/lib/draft-workspace-api";
import { cn } from "@/lib/utils";

interface AttachmentsPanelProps {
  workspace: DraftWorkspace;
  readOnly: boolean;
  leaseToken: string | null;
  flushAutosave: () => Promise<DraftWorkspace>;
  onWorkspaceChange: (workspace: DraftWorkspace) => void;
  onError: (message: string) => void;
}

type AttachmentOperation = {
  kind: "upload" | "remove";
  filename: string;
};

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function fileExtension(filename: string): string {
  const dotIndex = filename.lastIndexOf(".");
  return dotIndex > 0 ? filename.slice(dotIndex).toLowerCase() : "";
}

export function AttachmentsPanel({
  workspace,
  readOnly,
  leaseToken,
  flushAutosave,
  onWorkspaceChange,
  onError,
}: AttachmentsPanelProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const uploadHelpId = useId();
  const [dragging, setDragging] = useState(false);
  const [operation, setOperation] = useState<AttachmentOperation | null>(null);
  const limits = workspace.capabilities.attachment;
  const totalSize = workspace.attachments.reduce((sum, item) => sum + item.size, 0);
  const busy = operation !== null;

  const validate = (files: File[]) => {
    let projectedTotal = totalSize;
    for (const file of files) {
      const extension = fileExtension(file.name);
      if (!limits.allowed_extensions.includes(extension)) {
        throw new Error(`${file.name}: file type is not allowed.`);
      }
      if (file.size > limits.max_file_size) {
        throw new Error(
          `${file.name}: exceeds the ${formatBytes(limits.max_file_size)} per-file limit.`,
        );
      }
      projectedTotal += file.size;
    }
    if (projectedTotal > limits.max_total_size) {
      throw new Error(
        `Attachments would exceed the ${formatBytes(limits.max_total_size)} total limit.`,
      );
    }
  };

  const uploadFiles = async (files: File[]) => {
    if (!files.length || readOnly || busy) return;
    try {
      validate(files);
      setOperation({ kind: "upload", filename: files[0].name });
      let current = await flushAutosave();
      for (const file of files) {
        setOperation({ kind: "upload", filename: file.name });
        const result = await uploadWorkspaceAttachment(
          workspace.draft.draft_id,
          file,
          current.draft.revision.number,
          leaseToken,
        );
        current = result.workspace;
        onWorkspaceChange(current);
      }
    } catch (error) {
      onError(error instanceof Error ? error.message : "Attachment upload failed.");
    } finally {
      setOperation(null);
      setDragging(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  const remove = async (filename: string) => {
    if (readOnly || busy || !window.confirm(`Remove ${filename}?`)) return;
    setOperation({ kind: "remove", filename });
    try {
      const current = await flushAutosave();
      const result = await deleteWorkspaceAttachment(
        workspace.draft.draft_id,
        filename,
        current.draft.revision.number,
        leaseToken,
      );
      onWorkspaceChange(result.workspace);
    } catch (error) {
      onError(error instanceof Error ? error.message : "Could not remove attachment.");
    } finally {
      setOperation(null);
    }
  };

  return (
    <Card size="sm" id="attachments" className="border border-border/80 shadow-sm">
      <CardHeader className="border-b">
        <CardTitle className="flex items-center justify-between gap-3">
          <span className="flex items-center gap-2">
            <Paperclip className="size-4 text-primary" /> Attachments
          </span>
          <span
            className="font-sans text-xs font-normal text-muted-foreground"
            aria-label={`${formatBytes(totalSize)} of ${formatBytes(limits.max_total_size)} used`}
          >
            {formatBytes(totalSize)} / {formatBytes(limits.max_total_size)}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {!readOnly && (
          <div
            role="group"
            aria-label="Attachment upload"
            aria-busy={busy}
            className={cn(
              "flex min-h-28 flex-col items-center justify-center rounded-xl border border-dashed px-4 py-5 text-center transition-colors",
              dragging ? "border-primary bg-primary/5" : "border-border bg-muted/20",
              busy && "cursor-wait opacity-70",
            )}
            onDragEnter={(event) => {
              event.preventDefault();
              if (!busy) setDragging(true);
            }}
            onDragOver={(event) => {
              event.preventDefault();
              event.dataTransfer.dropEffect = busy ? "none" : "copy";
            }}
            onDragLeave={(event) => {
              const relatedTarget = event.relatedTarget;
              if (
                !(relatedTarget instanceof Node) ||
                !event.currentTarget.contains(relatedTarget)
              ) {
                setDragging(false);
              }
            }}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              if (!busy) void uploadFiles(Array.from(event.dataTransfer.files));
            }}
          >
            <UploadCloud className="mb-2 size-6 text-primary" />
            <p className="text-sm font-medium">Drop files here or choose from your device</p>
            <p id={uploadHelpId} className="mt-1 text-xs text-muted-foreground">
              {formatBytes(limits.max_file_size)} per file. {limits.allowed_extensions.join(", ")}
            </p>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="mt-3"
              disabled={busy}
              aria-describedby={uploadHelpId}
              onClick={() => inputRef.current?.click()}
            >
              Choose files
            </Button>
            <input
              ref={inputRef}
              hidden
              type="file"
              multiple
              accept={limits.allowed_extensions.join(",")}
              onChange={(event) => void uploadFiles(Array.from(event.target.files ?? []))}
            />
          </div>
        )}

        {workspace.attachments.length === 0 ? (
          <p className="py-2 text-center text-xs text-muted-foreground">No attachments.</p>
        ) : (
          <ul className="space-y-2" aria-label="Attached files">
            {workspace.attachments.map((attachment) => {
              const removing =
                operation?.kind === "remove" && operation.filename === attachment.filename;
              return (
                <li
                  key={attachment.filename}
                  className="flex items-center gap-3 rounded-lg border bg-background px-3 py-2.5"
                  aria-busy={removing}
                >
                  <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary">
                    <FileIcon className="size-4" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{attachment.filename}</p>
                    <p className="text-xs text-muted-foreground">
                      {formatBytes(attachment.size)}
                      {removing ? " · Removing…" : ""}
                    </p>
                  </div>
                  <Button
                    render={
                      <a
                        href={workspaceAttachmentDownloadUrl(
                          workspace.draft.draft_id,
                          attachment.filename,
                        )}
                        download
                      />
                    }
                    size="icon-xs"
                    variant="ghost"
                    aria-label={`Download ${attachment.filename}`}
                  >
                    <Download className="size-4" />
                  </Button>
                  {!readOnly && (
                    <Button
                      type="button"
                      size="icon-xs"
                      variant="destructive"
                      aria-label={`Remove ${attachment.filename}`}
                      disabled={busy}
                      onClick={() => void remove(attachment.filename)}
                    >
                      <Trash2 className="size-4" />
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        {operation && (
          <p className="text-xs text-primary" role="status">
            {operation.kind === "upload" ? "Uploading" : "Removing"} {operation.filename}…
          </p>
        )}
      </CardContent>
    </Card>
  );
}
