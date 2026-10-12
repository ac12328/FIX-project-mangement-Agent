"use client";

import { ArrowUpRight } from "lucide-react";

interface FollowUpCardsProps {
  questions: string[];
  disabled?: boolean;
  onQuestion: (question: string) => void;
}

export function FollowUpCards({
  questions,
  disabled = false,
  onQuestion,
}: FollowUpCardsProps) {
  if (!questions.length) return null;

  return (
    <div className="max-w-[42rem]" role="group" aria-label="Suggested follow-up questions" data-testid="assistant-follow-ups">
      <p className="mb-2 font-heading text-sm font-semibold text-foreground">Explore next</p>
      <div className="grid gap-2">
        {questions.slice(0, 3).map((question) => (
          <button
            key={question}
            type="button"
            disabled={disabled}
            onClick={() => onQuestion(question)}
            className="group flex min-h-11 w-full items-center gap-3 rounded-xl border border-border bg-card px-3.5 py-3 text-left text-sm font-medium leading-5 text-card-foreground transition-colors hover:border-primary/35 hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50"
          >
            <span className="min-w-0 flex-1">{question}</span>
            <ArrowUpRight className="size-4 shrink-0 text-primary transition-colors group-hover:text-accent-foreground" />
          </button>
        ))}
      </div>
    </div>
  );
}
