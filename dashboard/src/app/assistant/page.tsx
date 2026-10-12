"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { CalendarDays, ExternalLink, NotebookPen, RefreshCw, ShieldCheck } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { ErrorPanel, LoadingPanel } from "@/components/state-panel";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import {
  getCalendarEvents,
  getPersonalBrief,
  ingestMeetingNotes,
  prepareMeeting,
  proposeCalendarAction,
  refreshCalendar,
  type CalendarEvent,
  type MeetingPrep,
  type PersonalBrief,
} from "@/lib/api";
import { notifyError, notifySuccess } from "@/lib/notifications";

const labels: Record<string, string> = {
  needs_decision: "Needs decision",
  due_or_at_risk: "Due or at risk",
  meetings_to_prepare: "Meetings to prepare",
  waiting_on_others: "Waiting on others",
  important_messages: "Important messages",
  project_portfolio: "Project portfolio",
  fyi: "FYI",
};

export default function AssistantPage() {
  const [brief, setBrief] = useState<PersonalBrief | null>(null);
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [prep, setPrep] = useState<MeetingPrep | null>(null);
  const [notes, setNotes] = useState("");
  const [selectedEvent, setSelectedEvent] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    try {
      if (refresh) await refreshCalendar();
      const [nextBrief, nextEvents] = await Promise.all([
        getPersonalBrief(refresh),
        getCalendarEvents(7),
      ]);
      setBrief(nextBrief);
      setEvents(nextEvents);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load assistant data.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function prepare(event: CalendarEvent) {
    setBusy(`prep:${event.calendar_event_id}`);
    try {
      setPrep(await prepareMeeting(event.calendar_event_id));
      setSelectedEvent(event.calendar_event_id);
      setNotice("Meeting preparation generated from cached, attributable context.");
      notifySuccess("Meeting preparation ready", event.subject);
      setError("");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not prepare meeting.";
      setError(message);
      notifyError(err, message);
    } finally {
      setBusy("");
    }
  }

  async function ingest() {
    if (!selectedEvent || !notes.trim()) return;
    setBusy(`notes:${selectedEvent}`);
    try {
      const result = await ingestMeetingNotes(selectedEvent, notes);
      setNotice(`${result.work_item_ids.length} proposed work item(s) sent for review.`);
      notifySuccess("Meeting notes analyzed", `${result.work_item_ids.length} proposed work item(s) sent for review.`);
      setNotes("");
      await load();
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not ingest meeting notes.";
      setError(message);
      notifyError(err, message);
    } finally {
      setBusy("");
    }
  }

  async function stageCancel(event: CalendarEvent) {
    setBusy(`cancel:${event.calendar_event_id}`);
    try {
      await proposeCalendarAction({
        operation: "cancel",
        calendar_event_id: event.calendar_event_id,
      });
      setNotice("Calendar cancellation staged in the Approval Center.");
      notifySuccess("Cancellation staged", "Review it in the Approval Center before execution.");
      setError("");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not stage calendar action.";
      setError(message);
      notifyError(err, message);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="mx-auto max-w-[1600px] space-y-6">
      <PageHeader
        eyebrow="Work"
        title="Today"
        description="Prepare for upcoming meetings, review prioritized work, and stage governed calendar actions."
        metadata={!loading ? <>{events.length} upcoming event{events.length === 1 ? "" : "s"}{notice ? <><span aria-hidden="true">•</span>{notice}</> : null}</> : undefined}
        actions={
          <>
            <Button variant="outline" render={<Link href="/approvals" />}>
              <ShieldCheck className="size-4" />
              Approval Center
            </Button>
            <Button disabled={loading} onClick={() => void load(true)}>
              <RefreshCw className="size-4" />
              Refresh calendar
            </Button>
          </>
        }
      />

      {error ? <ErrorPanel description={error} onRetry={() => void load(true)} /> : null}
      {loading && !brief && !events.length ? <LoadingPanel label="Loading personal brief and calendar" /> : null}

      <Card className="border-border/80">
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-base"><CalendarDays className="size-4" /> Upcoming calendar</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 lg:grid-cols-2">
          {events.map((event) => (
            <div key={event.calendar_event_id} className="rounded-lg border bg-background p-3">
              <p className="font-medium">{event.subject}</p>
              <p className="mt-1 text-sm text-muted-foreground">
                {new Date(event.start_at).toLocaleString()} – {new Date(event.end_at).toLocaleTimeString()}
              </p>
              {event.location ? <p className="mt-1 text-xs text-muted-foreground">{event.location}</p> : null}
              <div className="mt-3 flex flex-wrap gap-2">
                <Button size="sm" variant="outline" disabled={Boolean(busy)} onClick={() => void prepare(event)}>
                  Prepare
                </Button>
                <Button size="sm" variant="outline" disabled={Boolean(busy)} onClick={() => {
                  setSelectedEvent(event.calendar_event_id);
                  setPrep(null);
                }}>
                  Add notes
                </Button>
                <Button size="sm" variant="destructive" disabled={Boolean(busy)} onClick={() => void stageCancel(event)}>
                  Propose cancel
                </Button>
                {event.join_url ? (
                  <a href={event.join_url} target="_blank" rel="noreferrer" className="inline-flex min-h-11 items-center justify-center gap-1 rounded-md px-3 text-sm font-medium hover:bg-accent hover:text-accent-foreground">
                    Join <ExternalLink className="size-3" />
                  </a>
                ) : null}
              </div>
            </div>
          ))}
          {!loading && !events.length ? <p className="text-sm text-muted-foreground">No upcoming cached events.</p> : null}
        </CardContent>
      </Card>

      {prep ? (
        <Card className="border-border/80">
          <CardHeader><CardTitle className="text-base">Meeting preparation — {prep.meeting.subject}</CardTitle></CardHeader>
          <CardContent className="grid gap-5 md:grid-cols-3">
            <PrepList title="Agenda" items={prep.agenda} />
            <PrepList title="Open decisions" items={prep.open_decisions} />
            <PrepList title="Talking points" items={prep.talking_points} />
          </CardContent>
        </Card>
      ) : null}

      {selectedEvent ? (
        <Card className="border-border/80">
          <CardHeader><CardTitle className="flex items-center gap-2 text-base"><NotebookPen className="size-4" /> Meeting notes</CardTitle></CardHeader>
          <CardContent className="space-y-3">
            <Textarea value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="Paste your meeting notes. Extracted items remain proposed until you confirm them." rows={7} />
            <div className="flex justify-end"><Button disabled={Boolean(busy) || !notes.trim()} onClick={() => void ingest()}>Extract proposed work</Button></div>
          </CardContent>
        </Card>
      ) : null}

      {brief ? (
        <div className="grid gap-4 lg:grid-cols-2">
          {Object.entries(brief.sections).map(([section, items]) => items.length ? (
            <Card key={section} className="border-border/80">
              <CardHeader className="pb-2"><CardTitle className="text-base">{labels[section] ?? section}</CardTitle></CardHeader>
              <CardContent className="space-y-3">
                {items.map((item) => (
                  <div key={`${section}-${item.id}`} className="rounded-lg border bg-background p-3">
                    <p className="font-medium">{item.title}</p>
                    <p className="mt-1 text-sm text-muted-foreground">{item.reason}</p>
                    {item.suggested_action ? <p className="mt-2 text-xs font-medium text-primary">{item.suggested_action}</p> : null}
                  </div>
                ))}
              </CardContent>
            </Card>
          ) : null)}
        </div>
      ) : <p className="text-sm text-muted-foreground">Loading personal brief…</p>}
    </div>
  );
}

function PrepList({ title, items }: { title: string; items: string[] }) {
  return (
    <div>
      <p className="text-sm font-medium">{title}</p>
      {items.length ? <ul className="mt-2 space-y-2 text-sm text-muted-foreground">{items.map((item) => <li key={item}>• {item}</li>)}</ul> : <p className="mt-2 text-sm text-muted-foreground">None identified.</p>}
    </div>
  );
}
