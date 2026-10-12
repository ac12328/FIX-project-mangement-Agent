"use client";

import { Suspense, type ReactNode } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { EmbeddedPage } from "@/lib/embedded-page";
import { updateSearchParams } from "@/lib/url-state";

export type WorkspaceTab = {
  id: string;
  label: string;
  content: ReactNode;
};

function WorkspaceTabsInner({
  tabs,
  param = "tab",
}: {
  tabs: WorkspaceTab[];
  param?: string;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const requested = searchParams.get(param);
  const active = tabs.some((tab) => tab.id === requested) ? requested! : tabs[0]?.id;

  function onChange(next: string | null) {
    if (!next || next === active) return;
    const query = updateSearchParams(searchParams, { [param]: next === tabs[0]?.id ? null : next });
    router.replace(`${pathname}${query ? `?${query}` : ""}`, { scroll: false });
  }

  return (
    <Tabs value={active} onValueChange={onChange}>
      <TabsList aria-label="Workspace views">
        {tabs.map((tab) => (
          <TabsTrigger key={tab.id} value={tab.id}>{tab.label}</TabsTrigger>
        ))}
      </TabsList>
      {tabs.map((tab) => (
        <TabsContent key={tab.id} value={tab.id} className="pt-4">
          {tab.id === active ? <EmbeddedPage>{tab.content}</EmbeddedPage> : null}
        </TabsContent>
      ))}
    </Tabs>
  );
}

export function WorkspaceTabs(props: { tabs: WorkspaceTab[]; param?: string }) {
  return (
    <Suspense fallback={null}>
      <WorkspaceTabsInner {...props} />
    </Suspense>
  );
}
