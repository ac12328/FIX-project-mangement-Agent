"use client";

import { PageHeader } from "@/components/page-header";
import { PartyDirectoryPage } from "@/components/party-directory-page";
import { WorkspaceTabs } from "@/components/workspace-tabs";

export default function CounterpartiesPage() {
  return (
    <div className="mx-auto max-w-[1400px] space-y-6">
      <PageHeader
        eyebrow="Counterparties"
        title="Counterparties"
        description="Clients and vendors."
      />
      <WorkspaceTabs
        tabs={[
          { id: "clients", label: "Clients", content: <PartyDirectoryPage kind="client" /> },
          { id: "vendors", label: "Vendors", content: <PartyDirectoryPage kind="vendor" /> },
        ]}
      />
    </div>
  );
}
