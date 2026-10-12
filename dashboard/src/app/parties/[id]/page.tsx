"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { Building2, Mail, Network, RefreshCw, UsersRound } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { ResponsiveList, ResponsiveListItem } from "@/components/responsive-list";
import { SectionPanel } from "@/components/section-panel";
import { ErrorPanel, LoadingPanel, StatePanel } from "@/components/state-panel";
import { StatusBadge } from "@/components/status-badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { getOrganisation } from "@/lib/api";

type PartyDetail = {
  organisation?: Record<string, unknown>;
  projects?: Array<Record<string, unknown>>;
  contacts?: Array<Record<string, unknown>>;
};

function display(value: unknown, fallback = "Unavailable"): string {
  if (value === null || value === undefined || value === "") return fallback;
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

export default function PartyDetailPage() {
  const params = useParams<{ id: string }>();
  const orgId = params.id;
  const [data, setData] = useState<PartyDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!orgId) return;
    setLoading(true);
    setError(null);
    try {
      setData(await getOrganisation(orgId));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load the organisation.");
    } finally {
      setLoading(false);
    }
  }, [orgId]);

  useEffect(() => { void load(); }, [load]);

  const organisation = data?.organisation;
  const projects = data?.projects ?? [];
  const contacts = data?.contacts ?? [];

  return (
    <div className="mx-auto max-w-[1500px] space-y-6">
      <PageHeader
        eyebrow="Parties"
        title={organisation ? display(organisation.org_name, orgId) : "Organisation detail"}
        description="Review the organisation profile, linked onboarding projects, and operational contacts."
        metadata={organisation ? (
          <>
            <span className="font-mono">{display(organisation.org_id, orgId)}</span>
            <span aria-hidden="true">•</span>
            <span>{display(organisation.domains, "No domains recorded")}</span>
          </>
        ) : undefined}
        actions={
          <>
            <Link href="/counterparties?tab=clients" className={buttonVariants({ variant: "outline" })}>Clients</Link>
            <Link href="/counterparties?tab=vendors" className={buttonVariants({ variant: "outline" })}>Vendors</Link>
            <Button variant="outline" onClick={() => void load()} disabled={loading}>
              <RefreshCw className={loading ? "size-4 animate-spin" : "size-4"} />
              Refresh
            </Button>
          </>
        }
      />

      {error ? <ErrorPanel description={error} onRetry={() => void load()} /> : null}
      {loading ? <LoadingPanel label="Loading organisation detail" /> : null}
      {!loading && !error && !organisation ? (
        <StatePanel
          icon={Building2}
          title="Organisation not found"
          description="This organisation may have been removed or the link may be invalid."
        />
      ) : null}

      {organisation ? (
        <div className="grid gap-6 xl:grid-cols-[minmax(0,1.2fr)_minmax(320px,0.8fr)]">
          <SectionPanel
            title="Linked projects"
            description={`${projects.length} onboarding project${projects.length === 1 ? "" : "s"} currently reference this organisation.`}
          >
            <div className="pt-5">
              {projects.length === 0 ? (
                <StatePanel icon={Network} title="No linked projects" description="No onboarding projects currently reference this organisation." />
              ) : (
                <ResponsiveList ariaLabel="Projects linked to this organisation">
                  {projects.map((project) => {
                    const projectId = display(project.project_id, display(project.id));
                    return (
                      <ResponsiveListItem
                        key={display(project.id, projectId)}
                        title={
                          <Link
                            href={`/projects/${encodeURIComponent(projectId)}`}
                            className="inline-flex min-h-11 items-center text-primary underline-offset-4 hover:underline"
                          >
                            {display(project.client_name, projectId)}
                          </Link>
                        }
                        metadata={<span className="font-mono">{projectId}</span>}
                        status={<StatusBadge value={display(project.party_role, "linked")} />}
                      >
                        <span>Organisation role: {display(project.party_role, "Not specified")}</span>
                      </ResponsiveListItem>
                    );
                  })}
                </ResponsiveList>
              )}
            </div>
          </SectionPanel>

          <div className="space-y-6">
            <SectionPanel title="Organisation profile" description="Canonical directory attributes used across projects and support work.">
              <dl className="grid gap-4 pt-5 sm:grid-cols-2 xl:grid-cols-1">
                <ProfileValue label="Organisation ID" value={display(organisation.org_id, orgId)} mono />
                <ProfileValue label="Type" value={display(organisation.org_type)} />
                <ProfileValue label="Domains" value={display(organisation.domains)} />
              </dl>
            </SectionPanel>

            <SectionPanel title="Contacts" description={`${contacts.length} operational contact${contacts.length === 1 ? "" : "s"} linked to this organisation.`}>
              <div className="pt-5">
                {contacts.length === 0 ? (
                  <StatePanel icon={UsersRound} title="No contacts linked" description="No contact records currently reference this organisation." />
                ) : (
                  <ResponsiveList ariaLabel="Organisation contacts">
                    {contacts.map((contact) => {
                      const email = display(contact.primary_email, "");
                      return (
                        <ResponsiveListItem
                          key={display(contact.contact_id, email || display(contact.display_name))}
                          title={display(contact.display_name, "Unnamed contact")}
                          metadata={display(contact.role, "Role not recorded")}
                          action={email ? (
                            <a href={`mailto:${email}`} className={buttonVariants({ variant: "outline", size: "sm" })}>
                              <Mail className="size-4" />Email
                            </a>
                          ) : undefined}
                        >
                          {email || "No email address recorded."}
                        </ResponsiveListItem>
                      );
                    })}
                  </ResponsiveList>
                )}
              </div>
            </SectionPanel>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ProfileValue({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
      <dd className={mono ? "mt-1 break-words font-mono text-sm" : "mt-1 break-words text-sm"}>{value}</dd>
    </div>
  );
}
