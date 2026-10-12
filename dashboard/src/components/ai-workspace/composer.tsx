"use client";

import { KeyboardEvent, useMemo, useRef, useState } from "react";
import {
  BriefcaseBusiness,
  Globe2,
  Plus,
  Send,
  Slash,
  Square,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { AssistantCommandCapability, AssistantThread } from "@/lib/assistant-types";
import { commandSuggestions, WORKSPACE_COMMANDS } from "@/lib/assistant-workspace";
import { cn } from "@/lib/utils";

interface WorkspaceComposerProps {
  thread: AssistantThread;
  value: string;
  projectLabels: Record<string, string>;
  capabilitiesCommands?: AssistantCommandCapability[];
  webAvailable: boolean;
  webReason?: string | null;
  sending: boolean;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
  onAddProjects: () => void;
  onRemoveProject: (projectId: string) => void;
  onToggleWeb: (enabled: boolean) => void;
}

export function WorkspaceComposer({
  thread,
  value,
  projectLabels,
  capabilitiesCommands,
  webAvailable,
  webReason,
  sending,
  onChange,
  onSubmit,
  onCancel,
  onAddProjects,
  onRemoveProject,
  onToggleWeb,
}: WorkspaceComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const suggestions = useMemo(() => {
    const local = commandSuggestions(value);
    if (!capabilitiesCommands?.length) return local;
    const byName = new Map(capabilitiesCommands.map((command) => [command.name, command]));
    return local.map((command) => byName.get(command.name) ?? command);
  }, [capabilitiesCommands, value]);


  function chooseCommand(name: string) {
    setActiveIndex(0);
    const needsArguments = ["resume", "project", "draft", "web"].includes(name);
    onChange(`/${name}${needsArguments ? " " : ""}`);
    window.setTimeout(() => textareaRef.current?.focus(), 0);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (suggestions.length) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActiveIndex((index) => (index + 1) % suggestions.length);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setActiveIndex((index) => (index - 1 + suggestions.length) % suggestions.length);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        onChange(value.replace(/^\//, ""));
        return;
      }
      if (event.key === "Enter") {
        const exact = suggestions.find((command) => value.trim() === `/${command.name}`);
        if (exact && exact.name !== "web") {
          event.preventDefault();
          if (!sending) onSubmit();
          return;
        }
      }
      if (event.key === "Tab" || event.key === "Enter") {
        event.preventDefault();
        chooseCommand(suggestions[activeIndex]?.name ?? suggestions[0].name);
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (value.trim() && !sending) onSubmit();
    }
  }

  const commandItems = capabilitiesCommands?.length ? capabilitiesCommands : WORKSPACE_COMMANDS;

  return (
    <div className="relative border-t bg-background/95 px-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-3 backdrop-blur sm:px-5">
      <div className="mx-auto max-w-4xl">
        {suggestions.length ? (
          <div
            role="listbox"
            aria-label="Slash commands"
            className="absolute bottom-[calc(100%-0.25rem)] left-3 right-3 z-20 max-h-72 overflow-y-auto rounded-xl border bg-popover p-1.5 text-popover-foreground shadow-xl sm:left-5 sm:right-5"
          >
            {suggestions.map((command, index) => (
              <button
                key={command.name}
                type="button"
                role="option"
                aria-selected={index === activeIndex}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => chooseCommand(command.name)}
                className={cn(
                  "flex min-h-11 w-full items-center gap-3 rounded-lg px-3 py-2 text-left transition-colors",
                  index === activeIndex ? "bg-accent text-accent-foreground" : "hover:bg-muted",
                )}
              >
                <span className="rounded-md bg-muted px-2 py-1 font-mono text-xs font-semibold">/{command.name}</span>
                <span className="min-w-0 flex-1 text-sm text-muted-foreground">{command.description}</span>
              </button>
            ))}
          </div>
        ) : null}

        <div className="mb-2 flex flex-wrap items-center gap-2" aria-label="Active assistant context">
          {thread.scope_mode === "portfolio" ? (
            <span className="inline-flex min-h-8 items-center gap-1.5 rounded-full border border-primary/25 bg-primary/8 px-3 text-xs font-medium text-primary">
              <BriefcaseBusiness className="size-3.5" />
              Full portfolio
            </span>
          ) : thread.project_ids.map((projectId) => (
            <span key={projectId} className="inline-flex min-h-8 max-w-56 items-center gap-1.5 rounded-full border bg-muted/60 pl-3 pr-1 text-xs font-medium">
              <span className="truncate">{projectLabels[projectId] || projectId}</span>
              <button
                type="button"
                aria-label={`Remove ${projectLabels[projectId] || projectId} from context`}
                onClick={() => onRemoveProject(projectId)}
                className="flex size-8 items-center justify-center rounded-full text-muted-foreground hover:bg-background hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <X className="size-3.5" />
              </button>
            </span>
          ))}
          <button
            type="button"
            onClick={onAddProjects}
            className="inline-flex min-h-8 items-center gap-1 rounded-full border border-dashed px-3 text-xs font-medium text-muted-foreground transition-colors hover:border-primary/40 hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Plus className="size-3.5" />
            Project
          </button>
          <button
            type="button"
            role="switch"
            aria-checked={thread.web_enabled}
            disabled={!webAvailable}
            title={!webAvailable ? webReason || "Web Search is unavailable" : undefined}
            onClick={() => onToggleWeb(!thread.web_enabled)}
            className={cn(
              "ml-auto inline-flex min-h-8 items-center gap-1.5 rounded-full border px-3 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50",
              thread.web_enabled ? "border-info/30 bg-info/10 text-info" : "text-muted-foreground hover:text-foreground",
            )}
          >
            <Globe2 className="size-3.5" />
            Web {thread.web_enabled ? "on" : "off"}
          </button>
        </div>

        <div className="rounded-2xl border bg-card p-2 shadow-sm focus-within:border-primary/40 focus-within:ring-3 focus-within:ring-ring/15">
          <label htmlFor="ai-workspace-composer" className="sr-only">Message AI Workspace</label>
          <Textarea
            ref={textareaRef}
            id="ai-workspace-composer"
            value={value}
            disabled={sending}
            onChange={(event) => {
              setActiveIndex(0);
              onChange(event.target.value);
            }}
            onKeyDown={handleKeyDown}
            placeholder="Ask about your portfolio or type / for commands"
            className="min-h-20 resize-none border-0 bg-transparent px-2 py-2 shadow-none focus-visible:ring-0"
          />
          <div className="flex items-center justify-between gap-2 border-t px-1 pt-2">
            <div className="flex items-center gap-1">
              <Button
                type="button"
                size="icon-sm"
                variant="ghost"
                aria-label="Open slash command palette"
                title={`${commandItems.length} commands available`}
                onClick={() => {
                  onChange("/");
                  textareaRef.current?.focus();
                }}
              >
                <Slash />
              </Button>
              <p className="hidden text-xs text-muted-foreground sm:block">Enter to send · Shift+Enter for a new line</p>
            </div>
            {sending ? (
              <Button type="button" variant="outline" size="sm" onClick={onCancel}>
                <Square className="fill-current" />
                Stop
              </Button>
            ) : (
              <Button type="button" size="sm" onClick={onSubmit} disabled={!value.trim()}>
                <Send />
                Send
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
