import type { Spec, Template, Version, Workflow } from './types';

/** Friendly picker summary derived from a catalog template (pure). */
export interface TemplateSummary {
  key: string;
  name: string;
  description: string;
  category: string;
  stepCount: number;
  stepLabels: string[];
  requiredInputs: string[];
}

export function templateSummary(template: Template): TemplateSummary {
  const spec: Spec = template.spec ?? { nodes: [], edges: [] };
  const nodes = spec.nodes ?? [];
  const inputs = spec.input_form ?? [];
  return {
    key: template.key,
    name: template.name,
    description: template.description,
    category: template.category ?? 'operations',
    stepCount: nodes.length,
    stepLabels: nodes.map(node => node.label),
    requiredInputs: inputs.filter(field => field.required).map(field => field.label),
  };
}

/** Editor state for a draft created from a template (pure). */
export interface TemplateDraftState {
  spec: Spec;
  errors: { code: string; message: string; node_id: string | null }[];
  requiresReview: boolean;
  workflowId: string;
  baseVersion: number;
}

export function templateDraftState(result: { workflow: Workflow; version: Version }): TemplateDraftState {
  return {
    spec: result.version.spec,
    errors: [],
    requiresReview: true,
    workflowId: result.workflow.id,
    baseVersion: result.version.number,
  };
}
