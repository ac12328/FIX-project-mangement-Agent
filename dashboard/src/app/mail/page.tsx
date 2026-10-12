"use client";

import ComposePage from "@/app/compose/page";
import DraftsPage from "@/app/drafts/page";
import InboxPage from "@/app/inbox/page";
import { PageHeader } from "@/components/page-header";
import { WorkspaceTabs } from "@/components/workspace-tabs";

export default function MailPage() {
  return (
    <div className="mx-auto max-w-[1600px] space-y-6">
      <PageHeader
        eyebrow="Mail"
        title="Mail"
        description="Onboarding mail that needs a reply, drafts, and compose."
      />
      <WorkspaceTabs
        tabs={[
          { id: "inbox", label: "Priority", content: <InboxPage /> },
          { id: "drafts", label: "Drafts", content: <DraftsPage /> },
          { id: "compose", label: "Compose", content: <ComposePage /> },
        ]}
      />
    </div>
  );
}
