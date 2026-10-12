import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** Strip common markdown control chars from bot-generated activity summary text. */
export function stripMarkdown(text: string): string {
  return text
    .replace(/\*\*(.+?)\*\*/g, "$1")   // **bold** → bold
    .replace(/\*(.+?)\*/g, "$1")        // *italic* → italic
    .replace(/[\u2500-\u257F]+/g, "")   // Unicode box-drawing chars (━ ═ ─ etc.)
    .replace(/\s+/g, " ")               // collapse whitespace
    .trim();
}
