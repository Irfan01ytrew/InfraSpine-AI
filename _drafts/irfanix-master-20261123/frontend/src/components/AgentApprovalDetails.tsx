import type { AgentStudioRun } from "../types";
import { formatDate } from "../format";

export function AgentApprovalDetails({ approval }: { approval: NonNullable<AgentStudioRun["pending_approval"]> }) {
  return <details className="mt-3 rounded-md border border-border bg-surface p-3">
    <summary className="cursor-pointer text-xs font-medium">Review exact action</summary>
    <p className="mt-2 text-xs">Tool: {approval.tool_name}</p>
    <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words text-xs">{JSON.stringify(approval.arguments, null, 2)}</pre>
    {approval.expires_at && <p className="mt-2 text-xs text-muted">Approval expires {formatDate(approval.expires_at)}</p>}
    <p className="mt-1 break-all font-mono text-[10px] text-muted">Action fingerprint: {approval.action_digest}</p>
  </details>;
}
