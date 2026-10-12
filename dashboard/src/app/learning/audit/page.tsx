"use client";

import { useCallback,useEffect,useState } from "react";
import { Button } from "@/components/ui/button";
import { Card,CardContent } from "@/components/ui/card";
import { LearningActionDialog } from "@/components/learning/learning-action-dialog";
import { LearningShell,LearningState } from "@/components/learning/learning-shell";
import { LearningTable } from "@/components/learning/learning-table";
import { getLearningAudit,getLearningKillSwitches,getLearningRollbacks,learningErrorState,LearningAudit,setLearningKillSwitch } from "@/lib/learning-api";
import { useEvents } from "@/lib/useEvents";

export default function AuditPage(){
 const[items,setItems]=useState<LearningAudit[]>([]);const[switches,setSwitches]=useState<Array<{name:string;effective:boolean}>>([]);const[rollbacks,setRollbacks]=useState(0);const[state,setState]=useState<"loading"|"ready"|"empty"|"error"|"forbidden"|"stale">("loading");const[selected,setSelected]=useState<{name:string;effective:boolean}|null>(null);
 const load=useCallback(async()=>{setState("loading");try{const[audit,flags,rollback]=await Promise.all([getLearningAudit(),getLearningKillSwitches(),getLearningRollbacks()]);setItems(audit.items);setSwitches(flags.items);setRollbacks(rollback.total);setState(audit.items.length||flags.items.length?"ready":"empty")}catch(error){setState(learningErrorState(error))}},[]);
 useEffect(()=>{const timer=window.setTimeout(()=>{void load()},0);return()=>window.clearTimeout(timer)},[load]);useEvents({eventTypes:["learning_candidate_changed","learning_fact_changed","learning_conflict_changed","learning_job_changed","learning_kill_switch_changed","learning_rollback_changed"],onRefresh:load});
 return <LearningShell title="Audit" description="Trace immutable governance IDs and operate emergency switches with a reason-bearing audit." active="/learning/audit">{state!=="ready"?<LearningState state={state} onRetry={load}/>:<><section aria-labelledby="effective-state"><h2 id="effective-state" className="mb-2 font-semibold">Effective operational state</h2><div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{switches.map((item)=><Card key={item.name}><CardContent className="p-3"><p className="font-mono text-xs">{item.name}</p><p className="text-sm font-medium">{item.effective?"Enabled":"Disabled"}</p>{item.name.includes("disabled")&&<Button className="mt-2" size="sm" variant="outline" onClick={()=>setSelected(item)}>Change</Button>}</CardContent></Card>)}<Card><CardContent className="p-3"><p className="text-xs text-muted-foreground">Rollback operations</p><p className="text-xl font-semibold">{rollbacks}</p></CardContent></Card></div></section><section aria-labelledby="audit-timeline"><h2 id="audit-timeline" className="mb-2 font-semibold">Audit timeline</h2>{items.length?<LearningTable headers={["Time","Actor","Action","Object","Outcome"]} rows={items.map((item)=>[item.created_at,item.actor,item.action_type,<span key="object" className="font-mono text-xs">{item.object_ref??"—"}</span>,item.summary])}/>:<LearningState state="empty"/>}</section></>}
 <LearningActionDialog open={Boolean(selected)} title="Change kill switch" description="The exact current switch state is included in the action-specific confirmation token." highRisk onClose={()=>setSelected(null)} onConfirm={async(reason)=>{if(!selected)return;await setLearningKillSwitch(selected.name,!selected.effective,reason,selected.effective);await load()}}/>
 </LearningShell>;
}
