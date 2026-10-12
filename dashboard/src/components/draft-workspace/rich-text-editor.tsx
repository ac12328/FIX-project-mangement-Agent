"use client";

import { useEffect } from "react";
import { EditorContent, useEditor } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import {
  Bold,
  Italic,
  Link2,
  List,
  ListOrdered,
  Redo2,
  RemoveFormatting,
  Undo2,
  Unlink,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const ALLOWED_LINK_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

function isSafeLink(href: string): boolean {
  try {
    return ALLOWED_LINK_PROTOCOLS.has(new URL(href).protocol);
  } catch {
    return false;
  }
}

interface RichTextEditorProps {
  value: string;
  onChange: (html: string) => void;
  readOnly?: boolean;
  invalid?: boolean;
}

export function RichTextEditor({ value, onChange, readOnly = false, invalid = false }: RichTextEditorProps) {
  const editor = useEditor({
    immediatelyRender: false,
    editable: !readOnly,
    extensions: [
      StarterKit.configure({
        blockquote: false,
        code: false,
        codeBlock: false,
        heading: false,
        horizontalRule: false,
        link: {
          autolink: true,
          linkOnPaste: true,
          openOnClick: false,
          isAllowedUri: isSafeLink,
          HTMLAttributes: { rel: "noopener noreferrer" },
        },
        strike: false,
      }),
    ],
    content: value || "<p></p>",
    editorProps: {
      attributes: {
        class:
          "min-h-[19rem] px-4 py-4 text-[15px] leading-7 outline-none prose prose-sm max-w-none dark:prose-invert prose-p:my-2 prose-ul:my-2 prose-ol:my-2 prose-a:text-primary",
        role: "textbox",
        "aria-invalid": invalid ? "true" : "false",
        "aria-label": "Email body",
        "aria-multiline": "true",
        "aria-readonly": readOnly ? "true" : "false",
      },
    },
    onUpdate: ({ editor: nextEditor }) => onChange(nextEditor.getHTML()),
  });

  useEffect(() => {
    if (!editor) return;
    editor.setEditable(!readOnly);
  }, [editor, readOnly]);

  useEffect(() => {
    if (!editor || editor.isFocused) return;
    const next = value || "<p></p>";
    if (editor.getHTML() !== next) editor.commands.setContent(next, { emitUpdate: false });
  }, [editor, value]);

  const setLink = () => {
    if (!editor) return;
    const existing = editor.getAttributes("link").href as string | undefined;
    const href = window.prompt("Enter an HTTP, HTTPS, or mailto link", existing ?? "https://");
    if (href === null) return;
    const nextHref = href.trim();
    if (!nextHref) {
      editor.chain().focus().extendMarkRange("link").unsetLink().run();
      return;
    }
    if (!isSafeLink(nextHref)) {
      window.alert("Use an HTTP, HTTPS, or mailto link.");
      return;
    }
    editor.chain().focus().extendMarkRange("link").setLink({ href: nextHref }).run();
  };

  const tools = [
    {
      label: "Bold",
      icon: Bold,
      active: editor?.isActive("bold"),
      run: () => editor?.chain().focus().toggleBold().run(),
    },
    {
      label: "Italic",
      icon: Italic,
      active: editor?.isActive("italic"),
      run: () => editor?.chain().focus().toggleItalic().run(),
    },
    {
      label: "Bulleted list",
      icon: List,
      active: editor?.isActive("bulletList"),
      run: () => editor?.chain().focus().toggleBulletList().run(),
    },
    {
      label: "Numbered list",
      icon: ListOrdered,
      active: editor?.isActive("orderedList"),
      run: () => editor?.chain().focus().toggleOrderedList().run(),
    },
  ];

  return (
    <div
      className={cn(
        "overflow-hidden rounded-xl border bg-background shadow-xs transition-colors focus-within:border-primary/60 focus-within:ring-3 focus-within:ring-primary/10",
        invalid && "border-danger/70 ring-3 ring-danger/10",
        readOnly && "bg-muted/25",
      )}
    >
      <div
        role="toolbar"
        aria-label="Formatting options"
        className="flex min-h-11 flex-wrap items-center gap-1 border-b bg-muted/35 px-2 py-1.5"
      >
        {tools.map(({ label, icon: Icon, active, run }) => (
          <Button
            key={label}
            type="button"
            size="icon-xs"
            variant={active ? "secondary" : "ghost"}
            aria-label={label}
            aria-pressed={Boolean(active)}
            title={label}
            disabled={readOnly || !editor}
            onClick={run}
          >
            <Icon className="size-4" />
          </Button>
        ))}
        <span className="mx-1 h-5 w-px bg-border" aria-hidden="true" />
        <Button
          type="button"
          size="icon-xs"
          variant={editor?.isActive("link") ? "secondary" : "ghost"}
          aria-label="Add or edit link"
          title="Add or edit link"
          disabled={readOnly || !editor}
          onClick={setLink}
        >
          <Link2 className="size-4" />
        </Button>
        <Button
          type="button"
          size="icon-xs"
          variant="ghost"
          aria-label="Remove link"
          title="Remove link"
          disabled={readOnly || !editor?.isActive("link")}
          onClick={() => editor?.chain().focus().unsetLink().run()}
        >
          <Unlink className="size-4" />
        </Button>
        <Button
          type="button"
          size="icon-xs"
          variant="ghost"
          aria-label="Clear formatting"
          title="Clear formatting"
          disabled={readOnly || !editor}
          onClick={() => editor?.chain().focus().clearNodes().unsetAllMarks().run()}
        >
          <RemoveFormatting className="size-4" />
        </Button>
        <span className="mx-1 h-5 w-px bg-border" aria-hidden="true" />
        <Button
          type="button"
          size="icon-xs"
          variant="ghost"
          aria-label="Undo"
          title="Undo"
          disabled={readOnly || !editor?.can().undo()}
          onClick={() => editor?.chain().focus().undo().run()}
        >
          <Undo2 className="size-4" />
        </Button>
        <Button
          type="button"
          size="icon-xs"
          variant="ghost"
          aria-label="Redo"
          title="Redo"
          disabled={readOnly || !editor?.can().redo()}
          onClick={() => editor?.chain().focus().redo().run()}
        >
          <Redo2 className="size-4" />
        </Button>
        <span className="ml-auto hidden text-xs text-muted-foreground sm:inline">
          Bold, italic, lists and safe links only
        </span>
      </div>
      <EditorContent editor={editor} />
    </div>
  );
}
