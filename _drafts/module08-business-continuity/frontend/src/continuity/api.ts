import type { Dashboard, Draft, Report, Workflow, Workspace } from './types';

export async function continuityRequest<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch('/api/continuity' + path, { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store', signal,
    headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-Requested-With': 'InfraSpineAdmin' },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(data?.error?.message ?? data?.detail?.map?.((d: { msg: string }) => d.msg).join('; ') ?? `Request failed (${response.status})`);
  return data as T;
}
export const workspacePath = (id: string) => '/tenants/' + encodeURIComponent(id);
export const bc = {
  workspaces: () => continuityRequest<{ items: Workspace[] }>('/tenants'),
  dashboard: (id: string, signal?: AbortSignal) => continuityRequest<Dashboard>(workspacePath(id) + '/dashboard', undefined, signal),
  workflows: (id: string) => continuityRequest<{ items: Workflow[] }>(workspacePath(id) + '/workflows'),
  handover: (id: string, scenarioId: string) => continuityRequest<Draft>(workspacePath(id) + '/scenarios/' + scenarioId + '/handover'),
  report: (id: string, runId: string, signal?: AbortSignal) => continuityRequest<Report>(workspacePath(id) + '/runs/' + runId, undefined, signal),
};

export function downloadJson(value: unknown, name: string) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }));
  const link = document.createElement('a'); link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
