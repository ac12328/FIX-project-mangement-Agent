"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { LearningActionDialog } from "@/components/learning/learning-action-dialog";
import { DetailSection, LearningDetailPanel } from "@/components/learning/learning-detail-panel";
import { LearningShell, LearningState } from "@/components/learning/learning-shell";
import { LearningTable, StatusPill } from "@/components/learning/learning-table";
import { actOnLearningFact, confirmation, factRevision, getLearningFact, getLearningFacts, learningErrorState, LearningFact } from "@/lib/learning-api";
import { useEvents } from "@/lib/useEvents";

const ACTIONS = ["deactivate", "revoke", "supersede", "change_expiry", "change_scope", "rollback"];
export default function MemoryPage() {
  const [items, setItems] = useState<LearningFact[]>([]); const [state, setState] = useState<"loading"|"ready"|"empty"|"error"|"forbidden"|"stale">("loading");
  const [selected, setSelected] = useState<LearningFact | null>(null); const [action, setAction] = useState("revoke"); const [detail, setDetail] = useState<Record<string,unknown>|null>(null);
  const load = useCallback(async () => { setState("loading"); try { const data = await getLearningFacts(); setItems(data.items); setState(data.items.length ? "ready" : "empty"); } catch (error) { setState(learningErrorState(error)); } }, []);
  useEffect(() => { const timer = window.setTimeout(() => { void load(); }, 0); return () => window.clearTimeout(timer); }, [load]); useEvents({ eventTypes: ["learning_fact_changed", "learning_rollback_changed"], onRefresh: load });
  const fields = action === "supersede" ? [{name:"candidate_id",label:"Replacement candidate ID",required:true},{name:"candidate_version",label:"Replacement candidate version",required:true}] : action === "change_expiry" ? [{name:"suggested_expiry",label:"New expiry (ISO 8601)",required:true}] : action === "change_scope" ? [{name:"scope_type",label:"New scope type",required:true},{name:"scope_id",label:"New scope ID",required:true}] : action === "rollback" ? [{name:"operation_id",label:"Rollback operation ID",required:true}] : [];
  const act = async (reason:string, _confirmed:boolean, values:Record<string,string>) => { if (!selected) return; const revision = action === "rollback" ? values.operation_id : factRevision(selected); const confirmed = await confirmation(action, selected.fact_id, revision); await actOnLearningFact(selected.fact_id, { action, reason, idempotency_key:crypto.randomUUID(), ...confirmed, operation_id:values.operation_id, replacement_candidate_id:values.candidate_id, replacement_candidate_version:values.candidate_version ? Number(values.candidate_version):undefined, scope_type:values.scope_type, scope_id:values.scope_id, suggested_expiry:values.suggested_expiry }); await load(); };
  return <LearningShell title="Memory" description="Immutable fact versions with provenance, validity, exposure, outcome, supersession, and rollback controls." active="/learning/memory">
    {state !== "ready" ? <LearningState state={state} onRetry={load} /> : <LearningTable headers={["Fact","Value","Scope","Validity","Controls"]} rows={items.map((item) => [<button key="id" className="font-mono text-xs underline underline-offset-2" onClick={async()=>setDetail(await getLearningFact(item.fact_id))}>{item.fact_id} v{item.fact_version}</button>, <span key="value">{item.predicate}: {item.value_json}</span>, <span key="scope">{item.scope_type}{item.scope_id ? ` · ${item.scope_id}`:""}</span>, <div key="validity"><StatusPill value={item.status}/><p className="mt-1 text-xs text-muted-foreground">{item.valid_from}{item.valid_to ? ` → ${item.valid_to}`:""}</p></div>, <div key="controls" className="flex max-w-sm flex-wrap gap-1">{ACTIONS.map((value)=><Button key={value} size="sm" variant="outline" onClick={()=>{setSelected(item);setAction(value)}}>{value}</Button>)}</div>])}/>}
    <LearningActionDialog open={Boolean(selected)} title={`${action} fact`} description="Fact changes are immutable candidate/lifecycle operations bound to the current fact revision." highRisk fields={fields} onClose={()=>setSelected(null)} onConfirm={act}/>
    {detail && <LearningDetailPanel title="Memory fact provenance and audit" onClose={()=>setDetail(null)}>{["fact","versions","decisions","state_timeline","lifecycle","exposures","outcomes","rollbacks","publications"].map((key)=><DetailSection key={key} title={key} value={detail[key]}/>)}</LearningDetailPanel>}
  </LearningShell>;
}
