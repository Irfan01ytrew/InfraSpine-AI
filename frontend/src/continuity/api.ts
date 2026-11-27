import type { Dashboard, Draft, Report, Workflow, Workspace } from './types';
import { request } from '../api';

export async function continuityRequest<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(body === undefined ? 'GET' : 'POST', '/api/continuity' + path, body, signal);
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
