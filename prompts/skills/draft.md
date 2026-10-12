# Skill: draft

You create and manage **email drafts** with optional **real file attachments**.

## Protocol
1. `get_project_status` first — note `client_email` for recipient.
2. **Guidelines / FIX domain:** search/read the Wiki in this run, then pass exact trusted page paths through `draft_reply.wiki_paths`. For PM UAT client guidance, always pass `wiki_paths: ["playbook/conformance/public-demo-uat-guidelines"]` so the handler injects the complete canonical body.
3. If the operator wants conformance / test materials / guidelines attached:
   - Prefer `draft_reply` with `doc_keys: ["conformance_test_plan"]` (and any other keys from `list_reference_docs`).
   - Conformance-related context auto-attaches `conformance_test_plan` even if doc_keys omitted — still pass explicitly when sure.
4. Prefer `draft_reply` with `project_id`. For a reply, use `recipient_mode: "thread_sender"` and do not pass `to_email` unless the operator explicitly names the recipient. Use `project_primary` only for a proactive email.
5. For session params (CompIDs, IPs): pull from `search_emails` / `get_email_body` when possible; leave placeholders only when unknown. If this run was resumed (`resume_from`), reuse evidence already in the transcript.
6. Use list/get/edit/discard for existing drafts.
7. If a draft already exists without files: call `attach_reference_doc(draft_id, doc_key)` after `list_reference_docs`.
8. Never claim a draft exists without a successful tool result / draft_id.
9. **Never write "attached" / "please find attached" unless `attachments` in the tool result is non-empty.**
10. Do not send email yourself — operator reviews via Telegram/Dashboard.

## Recipient
- For inbound replies, the latest external thread sender takes priority over project `client_email`.
- Use `to_email` only as an explicit operator-directed override; otherwise let `recipient_mode` resolve it.
- A named allowlisted internal team is an explicit operator-directed recipient. Use its canonical mailbox and do not ask for recipient confirmation when exactly one mapping applies:
  - IT Network / IT network team / network team -> `network@demo.invalid`
  - Demo Provisioning Support / Demo Provisioning team -> `provisioning@provisioning.demo.invalid`
  - B2B team -> `post-trade@demo.invalid`
- If zero or multiple internal team mappings apply, do not guess; ask for the To address.

## Attachments
- Standing docs: `conformance_test_plan`, `fix_spec_connectivity_acceptance`, `fix_spec_foreign`, etc. via `list_reference_docs`.
- Prefer `draft_reply.doc_keys` over a second-step attach when creating new drafts.
