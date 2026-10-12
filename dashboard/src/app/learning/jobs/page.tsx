"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { LearningActionDialog } from "@/components/learning/learning-action-dialog";
import { LearningShell, LearningState } from "@/components/learning/learning-shell";
import { LearningTable, StatusPill } from "@/components/learning/learning-table";
import { actOnLearningJob, confirmation, getLearningJobs, getLearningKillSwitches, jobRevision, learningErrorState, LearningJob, setLearningKillSwitch } from "@/lib/learning-api";
import { useEvents } from "@/lib/useEvents";

export default function JobsPage(){
 const[items,setItems]=useState<LearningJob[]>([]);const[switches,setSwitches]=useState<Array<{name:string;effective:boolean}>>([]);const[state,setState]=useState<"loading"|"ready"|"empty"|"error"|"forbidden"|"stale">("loading");const[selected,setSelected]=useState<LearningJob|null>(null);const[action,setAction]=useState("retry");const[kill,setKill]=useState<{name:string;effective:boolean}|null>(null);
 const load=useCallback(async()=>{setState("loading");try{const[data,flags]=await Promise.all([getLearningJobs(),getLearningKillSwitches()]);setItems(data.items);setSwitches(flags.items);setState(data.items.length||flags.items.length?"ready":"empty")}catch(error){setState(learningErrorState(error))}},[]);
 useEffect(()=>{const timer=window.setTimeout(()=>{void load()},0);return()=>window.clearTimeout(timer)},[load]);useEvents({eventTypes:["learning_job_changed","learning_kill_switch_changed"],onRefresh:load});
 return <LearningShell title="Jobs" description="Durable outbox controls honor lease/CAS rules, extractor identity, and audited emergency switches." active="/learning/jobs">
 {state!=="ready"?<LearningState state={state} onRetry={load}/>:<><section aria-label="Worker kill switches" className="mb-4 flex flex-wrap gap-2">{switches.filter((item)=>item.name==="worker_disabled"||item.name==="learning_writes_disabled").map((item)=><Button key={item.name} variant="outline" onClick={()=>setKill(item)}>{item.name}: {item.effective?"on":"off"}</Button>)}</section><LearningTable headers={["Job","Extractor","State","Failure","Controls"]} rows={items.map((item)=>[<span key="id" className="font-mono text-xs">{item.job_id}</span>,`${item.job_type} · ${item.extractor_version}`,<StatusPill key="state" value={item.state}/>,<span key="error" className="text-xs text-muted-foreground">{item.last_error??"—"}</span>,<div key="controls" className="flex gap-1">{["retry","quarantine","replay"].map((value)=><Button key={value} size="sm" variant="outline" onClick={()=>{setSelected(item);setAction(value)}}>{value}</Button>)}</div>])}/></>}
 <LearningActionDialog open={Boolean(selected)} title={`${action} job`} description="Replay requires a different extractor version. Cancel is intentionally omitted because this domain has no cancel terminal state." highRisk={action!=="retry"} fields={action==="replay"?[{name:"extractor_version",label:"New extractor version",required:true}]:[]} onClose={()=>setSelected(null)} onConfirm={async(reason,_confirmed,values)=>{if(!selected)return;const revision=jobRevision(selected);const confirmed=action!=="retry"?await confirmation(action,selected.job_id,revision):{};await actOnLearningJob(selected.job_id,{action,reason,idempotency_key:crypto.randomUUID(),...confirmed,extractor_version:values.extractor_version});await load()}}/>
 <LearningActionDialog open={Boolean(kill)} title="Change emergency kill switch" description="This audited control requires an explicit incident reason and is bound to the current effective state." highRisk onClose={()=>setKill(null)} onConfirm={async(reason)=>{if(!kill)return;await setLearningKillSwitch(kill.name,!kill.effective,reason,kill.effective);await load()}}/>
 </LearningShell>;
}
