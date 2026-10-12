"use client";

import { useEffect, useRef, useState } from "react";
import { Paperclip, RefreshCw, Upload, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { deleteAttachment, listAttachments, uploadAttachment } from "@/lib/api";
import type { Attachment } from "@/lib/types";

interface DraftAttachmentsCardProps {
  draftId: string;
  readOnly: boolean;
}

export function DraftAttachmentsCard({ draftId, readOnly }: DraftAttachmentsCardProps) {
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let cancelled = false;
    listAttachments(draftId)
      .then((items) => {
        if (!cancelled) setAttachments(items);
      })
      .catch(() => null);
    return () => {
      cancelled = true;
    };
  }, [draftId]);

  const refresh = async () => {
    try {
      setAttachments(await listAttachments(draftId));
      setError(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Refresh failed");
    }
  };

  const upload = async (file: File) => {
    setUploading(true);
    setError(null);
    try {
      const attachment = await uploadAttachment(draftId, file);
      setAttachments((current) => [...current, attachment]);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  };

  const remove = async (filename: string) => {
    try {
      await deleteAttachment(draftId, filename);
      setAttachments((current) => current.filter((item) => item.filename !== filename));
      setError(null);
    } catch {
      setError("Failed to delete attachment");
    }
  };

  return (
    <Card size="sm" className="border border-border/80 bg-card shadow-sm">
      <CardHeader className="border-b border-border/70">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2">
            <Paperclip className="size-4 text-muted-foreground" />
            Attachments
            <Badge variant="outline" className="text-xs">
              {attachments.length}
            </Badge>
          </CardTitle>
          {!readOnly && (
            <div className="flex items-center gap-1">
              <Button
                size="icon-xs"
                variant="ghost"
                onClick={refresh}
                title="Refresh attachments"
                aria-label="Refresh attachments"
              >
                <RefreshCw className="size-3.5" />
              </Button>
              <Button
                size="xs"
                variant="outline"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
              >
                <Upload className="size-3.5" />
                {uploading ? "Uploading..." : "Add file"}
              </Button>
            </div>
          )}
        </div>
        {!readOnly && (
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            onChange={async (event) => {
              const file = event.target.files?.[0];
              if (file) await upload(file);
              event.target.value = "";
            }}
          />
        )}
      </CardHeader>
      <CardContent className="space-y-2">
        {error && <div className="text-xs text-danger">{error}</div>}
        {attachments.length > 0 ? (
          <div className="space-y-2">
            {attachments.map((attachment) => (
              <div
                key={attachment.filename}
                className="flex items-center gap-2 rounded-lg border border-border/70 bg-background p-2.5"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-xs" title={attachment.filename}>
                    {attachment.filename}
                  </div>
                  <div className="mt-0.5 text-xs text-muted-foreground">
                    {formatFileSize(attachment.size)}
                  </div>
                </div>
                {!readOnly && (
                  <Button
                    size="icon-xs"
                    variant="ghost"
                    className="text-muted-foreground hover:text-danger"
                    aria-label={`Remove ${attachment.filename}`}
                    onClick={() => remove(attachment.filename)}
                  >
                    <X className="size-3.5" />
                  </Button>
                )}
              </div>
            ))}
          </div>
        ) : (
          <p className="text-xs leading-5 text-muted-foreground">No attachments added.</p>
        )}
        <p className="text-xs leading-4 text-muted-foreground">
          Max 10MB per file and 25MB total. PDF, Office, text, image, config, log, and ZIP files are supported.
        </p>
      </CardContent>
    </Card>
  );
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
