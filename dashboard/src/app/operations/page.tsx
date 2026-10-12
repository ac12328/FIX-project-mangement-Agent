"use client";

import Link from "next/link";
import { FileText } from "lucide-react";

import CompletedProjectsPanel from "@/app/operations/completed-projects";
import PortfolioPage from "@/app/portfolio/page";
import TimelinePage from "@/app/timeline/page";
import { PageHeader } from "@/components/page-header";
import { WorkspaceTabs } from "@/components/workspace-tabs";
import { Button } from "@/components/ui/button";

export default function OperationsPage() {
  return (
    <div className="mx-auto max-w-[1600px] space-y-6">
      <PageHeader
        eyebrow="Operations"
        title="Operations"
        description="Portfolio risk, go-live dates, and recently completed projects. Export a report when you need a snapshot."
        actions={
          <>
          <Button render={<Link href="/reports/projects" />}>Project summary</Button>
          <Button variant="outline" render={<Link href="/reports" />}>
            <FileText className="size-4" />
            Export report
          </Button>
          </>
        }
      />
      <WorkspaceTabs
        tabs={[
          { id: "health", label: "Portfolio health", content: <PortfolioPage /> },
          { id: "timeline", label: "Delivery timeline", content: <TimelinePage /> },
          { id: "completed", label: "Completed", content: <CompletedProjectsPanel /> },
        ]}
      />
    </div>
  );
}
