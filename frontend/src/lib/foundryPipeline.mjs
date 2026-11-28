// Step AM slice 6 — build-pipeline view for the Foundry page.
//
// Pure helpers (no React, no network) so they are unit-tested with
// `node --test test/foundryPipeline.test.mjs`. `buildFoundryPipelineSpec`
// turns one registered app's latest build into a workflow `Spec` that the
// existing `FlowDiagram` component renders read-only (no `onChange`):
// spec -> validate -> generate -> test -> preview -> approve -> deploy ->
// feedback -> improve. A live app is never patched in place, so the
// "improve" stage always points at a *new* revision, never at the live build.

export const PIPELINE_STAGES = [
  { id: "spec", label: "Spec", description: "Structured app spec: pages, endpoints, tables." },
  { id: "validate", label: "Validate", description: "Slice 1: unsafe specs are rejected with a named reason." },
  { id: "generate", label: "Generate", description: "Slice 2: interface, backend and database as data only." },
  { id: "test", label: "Test", description: "Slice 3: generated tests run in the QA sandbox." },
  { id: "preview", label: "Preview", description: "Slice 3: disposable preview, expiring link, seed data only." },
  { id: "approve", label: "Approve", description: "Slice 4: user approves the exact build hash. Approval is single-use." },
  { id: "deploy", label: "Deploy", description: "Slice 4: deploy only after approval; failed health rolls back." },
  { id: "feedback", label: "Feedback", description: "Slice 5: feedback attaches to one build version." },
  { id: "improve", label: "Improve", description: "Slice 5: a new spec revision re-enters the pipeline. Live is never patched." },
];

export function stageStatus(build, feedbackCount = 0) {
  const states = {};
  const hasBuild = Boolean(build);
  states.spec = hasBuild ? "done" : "pending";
  states.validate = hasBuild ? "done" : "pending";
  states.generate = hasBuild ? "done" : "pending";
  if (!hasBuild) {
    states.test = "pending";
    states.preview = "pending";
    states.approve = "pending";
    states.deploy = "pending";
  } else {
    states.test =
      build.tests_failed > 0 ? "blocked" : build.tests_passed > 0 ? "done" : "pending";
    states.preview =
      build.gate === "preview_ready"
        ? "done"
        : build.health === "failed"
          ? "blocked"
          : "pending";
    states.approve =
      build.approval_id || ["approved", "deploying", "deployed", "rolled_back"].includes(build.deploy_state)
        ? "done"
        : build.deploy_state === "requested"
          ? "active"
          : "pending";
    states.deploy =
      build.deploy_state === "deployed"
        ? "done"
        : build.deploy_state === "deploying"
          ? "active"
          : ["rolled_back", "failed"].includes(build.deploy_state)
            ? "blocked"
            : "pending";
  }
  states.feedback = feedbackCount > 0 ? "done" : "pending";
  states.improve = build && build.spec_version > 1 ? "done" : "pending";
  return states;
}

export function buildFoundryPipelineSpec(appDetail) {
  const builds = appDetail?.builds ?? [];
  const latest = builds.length ? builds[builds.length - 1] : null;
  const feedbackCount = (appDetail?.feedback ?? []).length;
  const statuses = stageStatus(latest, feedbackCount);
  const nodes = PIPELINE_STAGES.map((stage, index) => {
    const type = index === 0 ? "start" : stage.id === "approve" ? "approval" : stage.id === "improve" ? "end" : "task";
    return {
      id: `stage-${stage.id}`,
      type,
      label: `${stage.label} · ${statuses[stage.id]}`,
      x: 100 + index * 260,
      y: 100,
      risk: stage.id === "deploy" ? "medium" : "low",
      config: { instructions: stage.description },
    };
  });
  const edges = PIPELINE_STAGES.slice(1).map((stage, index) => ({
    source: `stage-${PIPELINE_STAGES[index].id}`,
    target: `stage-${stage.id}`,
    branch: "always",
  }));
  return { schema_version: 1, nodes, edges, source: "ai" };
}
