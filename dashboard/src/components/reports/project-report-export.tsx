"use client";

import { useEffect, useRef, useState, type RefObject } from "react";
import { Copy, Image as ImageIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { projectReportMarkdown, type ProjectReport } from "@/lib/project-report";
import { reportImagePng, writeReportImageClipboard } from "@/lib/project-report-image";

export function ProjectReportExport({ report, svgRef, disabled, onBusyChange }: {
  report: ProjectReport; svgRef: RefObject<SVGSVGElement | null>; disabled: boolean;
  onBusyChange: (busy: boolean) => void;
}) {
  const [preview, setPreview] = useState(false);
  const [manualCopy, setManualCopy] = useState(false);
  const [fallbackUrl, setFallbackUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const textRef = useRef<HTMLTextAreaElement>(null);
  const markdown = projectReportMarkdown(report);
  useEffect(() => () => { if (fallbackUrl) URL.revokeObjectURL(fallbackUrl); }, [fallbackUrl]);
  useEffect(() => {
    if (preview && manualCopy) {
      const timer = setTimeout(() => { textRef.current?.focus(); textRef.current?.select(); }, 100);
      return () => clearTimeout(timer);
    }
  }, [preview, manualCopy]);

  async function copyMarkdown() {
    try {
      await navigator.clipboard.writeText(markdown);
      setMessage("Markdown copied. Paste it first, then copy and paste the overview image.");
    } catch {
      setManualCopy(true); setPreview(true);
      setMessage("Automatic text copy is unavailable. Select the report text and press Ctrl+C / Cmd+C.");
    }
  }
  async function copyImage() {
    if (!svgRef.current) return;
    setBusy(true); onBusyChange(true); setMessage("");
    const png = reportImagePng(svgRef.current);
    // Observe generation failures even when the clipboard API is unavailable.
    void png.catch(() => {});
    try {
      await writeReportImageClipboard(png);
      setMessage("Overview image copied. Paste into your case now (Ctrl+V / Cmd+V), before copying anything else.");
    } catch {
      try {
        setFallbackUrl(URL.createObjectURL(await png));
        setMessage("Automatic image copy was unavailable or denied. Right-click the preview and choose Copy image. Nothing was downloaded.");
      } catch (error) { setMessage(error instanceof Error ? error.message : "Could not create the image. Please retry."); }
    } finally { setBusy(false); onBusyChange(false); }
  }
  return <div className="min-w-0 space-y-2">
    <div className="flex flex-wrap items-center gap-2">
      <Button variant="outline" size="sm" disabled={disabled || busy} onClick={() => void copyImage()}><ImageIcon className="size-4" />{busy ? "Copying image..." : "Copy Overview Image"}</Button>
      <Button variant="outline" size="sm" aria-label="Preview Markdown" title="Preview Markdown" disabled={disabled || busy} onClick={() => { setManualCopy(false); setPreview(true); }}><span className="sm:hidden">Preview</span><span className="hidden sm:inline">Preview Markdown</span></Button>
      <Button size="sm" disabled={disabled || busy} onClick={() => void copyMarkdown()}><Copy className="size-4" />Copy as Markdown</Button>
    </div>
    <p className="text-xs text-muted-foreground">Paste Markdown first, then copy and paste the image. No download.</p>
    {message && <p role="status" className="text-sm">{message}</p>}
    <Dialog open={preview} onOpenChange={setPreview}>
      <DialogContent className="max-h-[90vh] max-w-5xl overflow-y-auto">
        <DialogHeader><DialogTitle>Markdown report</DialogTitle><DialogDescription>All three sections from the displayed snapshot. Paste the report first, then copy the overview image separately.</DialogDescription></DialogHeader>
        {manualCopy && <p role="status" className="mt-3 text-sm">Select the text below and press Ctrl+C / Cmd+C.</p>}
        <Textarea ref={textRef} className="my-4 h-[50vh] font-mono text-xs" aria-label="Report Markdown" wrap="off" readOnly value={markdown} onFocus={event => { if (manualCopy) event.currentTarget.select(); }} />
        <Button variant="outline" size="sm" disabled={disabled || busy} onClick={() => void copyMarkdown()}>Copy Markdown</Button>
      </DialogContent>
    </Dialog>
    <Dialog open={!!fallbackUrl} onOpenChange={open => { if (!open) setFallbackUrl(null); }}>
      <DialogContent className="max-h-[90vh] max-w-6xl overflow-y-auto">
        <DialogHeader><DialogTitle>Copy overview image manually</DialogTitle><DialogDescription>Automatic copy is unavailable or was denied. Right-click the image and choose Copy image, then paste into your case. Nothing was downloaded.</DialogDescription></DialogHeader>
        {/* A local blob preview must remain a native image for the browser's Copy image menu. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        {fallbackUrl && <img src={fallbackUrl} alt={`Project milestone overview dated ${report.report_date}`} className="mt-4 h-auto w-full rounded border" />}
      </DialogContent>
    </Dialog>
  </div>;
}
