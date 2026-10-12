"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { LearningActionDialog } from "@/components/learning/learning-action-dialog";
import { LearningShell, LearningState } from "@/components/learning/learning-shell";
import { LearningTable, StatusPill } from "@/components/learning/learning-table";
import { actOnLearningConflict, confirmation, conflictRevision, getLearningConflicts, learningErrorState, LearningConflict } from "@/lib/learning-api";
import { useEvents } from "@/lib/useEvents";

export default function ConflictsPage() {
  const [items,setItems]=useState<LearningConflict[]>([]); const [state,setState]=useState<"loading"|"ready"|"empty"|"error"|"forbidden"|"stale">("loading"); const [selected,setSelected]=useState<LearningConflict|null>(null); const [action,setAction]=useState("resolve");
  const load=useCallback(async()=>{setState("loading");try{const data=await getLearningConflicts();setItems(data.items);setState(data.items.length?"ready":"empty")}catch(error){setState(learningErrorState(error))}},[]);
  useEffect(()=>{const timer=window.setTimeout(()=>{void load()},0);return()=>window.clearTimeout(timer)},[load]); useEvents({eventTypes:["learning_conflict_changed"],onRefresh:load});
  const fields=action==="resolve"?[]:[{name:"candidate_id",label:"Selected member candidate ID",required:true},{name:"candidate_version",label:"Candidate version",required:true}];
  return <LearningShell title="Conflicts" description="Resolve typed contradictions and merge only validated equivalent members." active="/learning/conflicts">
    {state!=="ready"?<LearningState state={state} onRetry={load}/>:<LearningTable headers={["Conflict","Canonical key","Risk","Authority","Controls"]} rows={items.map((item)=>[<span key="id" className="font-mono text-xs">{item.conflict_id}</span>,item.canonical_key,<StatusPill key="risk" value={item.risk_level}/>,item.authority_blocked?"Blocked":"Advisory",<div key="controls" className="flex gap-1">{["resolve","merge","supersede"].map((value)=><Button key={value} size="sm" variant="outline" onClick={()=>{setSelected(item);setAction(value)}}>{value}</Button>)}</div>])}/>}
    <LearningActionDialog open={Boolean(selected)} title={`${action} conflict`} description="Multi-operator conflicts are rejected unless an authorized governance-admin policy applies." highRisk={action!=="resolve" || selected?.risk_level==="high" || selected?.risk_level==="critical"} fields={fields} onClose={()=>setSelected(null)} onConfirm={async(reason,_confirmed,values)=>{if(!selected)return;const revision=conflictRevision(selected);const confirmed=(action!=="resolve"||["high","critical"].includes(selected.risk_level))?await confirmation(action,selected.conflict_id,revision):{};await actOnLearningConflict(selected.conflict_id,{action,reason,idempotency_key:crypto.randomUUID(),...confirmed,candidate_id:values.candidate_id,candidate_version:values.candidate_version?Number(values.candidate_version):undefined});await load()}}/>
  </LearningShell>;
}
