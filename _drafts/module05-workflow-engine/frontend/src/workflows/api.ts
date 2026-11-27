import type { Adapter, Dashboard, Member, Run, RunDetail, Scope, Spec, Template, Validation, Workflow, WorkflowDetail, Version } from './types';

const root = '/api/workflow-engine';
async function call<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(root + path, { method, credentials: 'same-origin',
    headers: { 'X-Requested-With': 'InfraSpine', ...(body === undefined ? {} : {'Content-Type':'application/json'}) },
    ...(body === undefined ? {} : {body: JSON.stringify(body)}) });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.error?.message || `Workflow request failed (${response.status}).`);
  }
  return response.json() as Promise<T>;
}
const scope = (id: string) => `/scopes/${encodeURIComponent(id)}`;
export const workflowApi = {
  scopes: () => call<{scopes:Scope[]}>('/scopes'),
  templates: () => call<{templates:Template[];adapters:Adapter[]}>('/templates'),
  members: (s:string) => call<{members:Member[]}>(scope(s)+'/members'),
  dashboard: (s:string) => call<Dashboard>(scope(s)+'/dashboard'),
  workflows: (s:string,q='',offset=0) => call<{workflows:Workflow[];total:number}>(scope(s)+`/workflows?q=${encodeURIComponent(q)}&offset=${offset}`),
  workflow: (s:string,id:string) => call<WorkflowDetail>(scope(s)+`/workflows/${id}`),
  save: (s:string,name:string,description:string,spec:Spec,id?:string,base?:number) => call<{workflow:Workflow;version:Version}>(scope(s)+(id?`/workflows/${id}/versions`:'/workflows'),'POST',{name,description,spec,base_version:base}),
  validate: (s:string,spec:Spec) => call<{valid:boolean;errors:Validation[]}>(scope(s)+'/validate','POST',spec),
  draft: (s:string,requirements:string) => call<{spec:Spec;errors:Validation[];requires_review:boolean}>(scope(s)+'/draft-from-text','POST',{requirements}),
  review: (s:string,id:string,version_id:string) => call<Version>(scope(s)+`/workflows/${id}/review`,'POST',{version_id}),
  reviewDecision: (s:string,id:string,version_id:string,approved:boolean,note:string) => call<Version>(scope(s)+`/workflows/${id}/review-decision`,'POST',{version_id,approved,note}),
  publish: (s:string,id:string,version_id:string) => call<Version>(scope(s)+`/workflows/${id}/publish`,'POST',{version_id}),
  disable: (s:string,id:string) => call<Workflow>(scope(s)+`/workflows/${id}/disable`,'POST'),
  trigger: (s:string,id:string,inputs:Record<string,unknown>,simulation=false,version_id?:string,key=crypto.randomUUID()) => call<Run>(scope(s)+`/workflows/${id}/runs`,'POST',{inputs,simulation,version_id,idempotency_key:key}),
  runs: (s:string,status='',offset=0) => call<{runs:Run[];total:number}>(scope(s)+`/runs?offset=${offset}${status?'&status='+encodeURIComponent(status):''}`),
  run: (s:string,id:string) => call<RunDetail>(scope(s)+`/runs/${id}`),
  control: (s:string,id:string,command:string) => call<Run>(scope(s)+`/runs/${id}/control/${command}`,'POST'),
  complete: (s:string,r:string,id:string,output:Record<string,unknown>) => call(scope(s)+`/runs/${r}/steps/${id}/complete`,'POST',{output}),
  decide: (s:string,r:string,id:string,approved:boolean,note:string,evidence:Record<string,unknown>,on_behalf_of?:string) => call(scope(s)+`/runs/${r}/steps/${id}/decision`,'POST',{approved,note,evidence,on_behalf_of}),
  reassign: (s:string,r:string,id:string,assignee_id:string,note:string) => call(scope(s)+`/runs/${r}/steps/${id}/reassign`,'POST',{assignee_id,note}),
  signal: (s:string,r:string,name:string,payload:Record<string,unknown>) => call(scope(s)+`/runs/${r}/signals`,'POST',{name,payload,idempotency_key:crypto.randomUUID()}),
  event: (s:string,name:string,payload:Record<string,unknown>) => call<{run_ids:string[]}>(scope(s)+'/events','POST',{name,payload,idempotency_key:crypto.randomUUID()}),
  readNotice: (s:string,id:string) => call(scope(s)+`/notices/${id}/read`,'POST'),
  exportUrl: (s:string) => root+scope(s)+'/export',
};
