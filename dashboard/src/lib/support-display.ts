const PREFIXES = [
  "re:", "fw:", "fwd:", "re：", "fw：",
  "回复:", "回复：", "回覆:", "回覆：",
  "答复:", "答复：", "答覆:", "答覆：",
  "转发:", "转发：", "轉發:", "轉發：",
];
const TOKENS = ["[bulk]", "[external email]", "[external]", "[外部郵件]"];

export function cleanDisplayTitle(title: string | null | undefined): string {
  let text = (title ?? "").trim();
  let changed = true;
  while (changed && text) {
    changed = false;
    const lowered = text.toLowerCase();
    for (const prefix of PREFIXES) {
      if (lowered.startsWith(prefix)) {
        text = text.slice(prefix.length).trim();
        changed = true;
        break;
      }
    }
    const next = text.toLowerCase();
    for (const token of TOKENS) {
      if (next.startsWith(token)) {
        text = text.slice(token.length).trim();
        changed = true;
        break;
      }
    }
  }
  return text.replace(/\s+/g, " ").trim() || (title ?? "");
}

export function partyFromEmail(email: string | null | undefined): string | null {
  if (!email || !email.includes("@")) return null;
  const domain = email.split("@")[1]?.trim().toLowerCase();
  if (!domain) return null;
  const internal = new Set(["demo.invalid", "provisioning.demo.invalid", "acceptance.demo.invalid"]);
  if (internal.has(domain)) return null;
  return domain;
}

export function deskBriefingFromDetail(detail: {
  ticket_id: string;
  title: string;
  counterparty?: string | null;
  counterparty_org_name?: string | null;
  reporter_email?: string | null;
  assigned_team?: string | null;
  conversation?: Array<{ direction: string; preview?: string | null; content_text?: string | null }>;
}): TicketAISummaryShape {
  const latest = detail.conversation?.[0];
  const preview = (latest?.content_text || latest?.preview || "").replace(/\s+/g, " ").trim();
  return {
    ticket_id: detail.ticket_id,
    from_party: detail.counterparty_org_name || detail.counterparty || partyFromEmail(detail.reporter_email) || "Unknown",
    to_party: detail.assigned_team || "Desk",
    what_happened: preview.slice(0, 240) || cleanDisplayTitle(detail.title),
    recommended_action: "send_reply",
  };
}

export type TicketAISummaryShape = {
  ticket_id: string;
  from_party: string;
  to_party: string;
  what_happened: string;
  reply_draft_text?: string;
  recommended_action?: "send_reply" | "close_directly" | "investigate";
};
