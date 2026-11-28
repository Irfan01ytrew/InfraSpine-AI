import test from "node:test";
import assert from "node:assert/strict";
import { PIPELINE_STAGES, buildFoundryPipelineSpec, stageStatus } from "../src/lib/foundryPipeline.mjs";

const readyBuild = {
  spec_version: 1,
  gate: "preview_ready",
  health: "passed",
  tests_passed: 6,
  tests_failed: 0,
  deploy_state: "deployed",
  approval_id: "approval-1",
};

test("pipeline has the nine foundry stages in order", () => {
  assert.deepEqual(
    PIPELINE_STAGES.map((s) => s.id),
    ["spec", "validate", "generate", "test", "preview", "approve", "deploy", "feedback", "improve"],
  );
});

test("stage statuses follow the latest build state", () => {
  const statuses = stageStatus(readyBuild, 2);
  assert.equal(statuses.spec, "done");
  assert.equal(statuses.test, "done");
  assert.equal(statuses.preview, "done");
  assert.equal(statuses.approve, "done");
  assert.equal(statuses.deploy, "done");
  assert.equal(statuses.feedback, "done");
  assert.equal(statuses.improve, "pending"); // version 1: no revision yet
});

test("a failed test run blocks the test stage and the preview", () => {
  const statuses = stageStatus({ ...readyBuild, tests_failed: 2, tests_passed: 4, gate: "blocked", health: "failed", deploy_state: "none", approval_id: null });
  assert.equal(statuses.test, "blocked");
  assert.equal(statuses.preview, "blocked");
  assert.equal(statuses.approve, "pending");
  assert.equal(statuses.deploy, "pending");
});

test("a requested deploy is the active approval stage", () => {
  const statuses = stageStatus({ ...readyBuild, deploy_state: "requested", approval_id: "approval-1" });
  assert.equal(statuses.approve, "done");
  assert.equal(statuses.deploy, "pending");
});

test("a rolled-back deploy blocks the deploy stage", () => {
  const statuses = stageStatus({ ...readyBuild, deploy_state: "rolled_back" });
  assert.equal(statuses.deploy, "blocked");
  assert.equal(statuses.approve, "done");
});

test("no build means every stage is pending", () => {
  const statuses = stageStatus(null, 0);
  assert.ok(Object.values(statuses).every((s) => s === "pending"));
});

test("a second spec version marks the improve stage done", () => {
  const statuses = stageStatus({ ...readyBuild, spec_version: 2 }, 1);
  assert.equal(statuses.improve, "done");
});

test("pipeline spec chains the stages for the read-only diagram", () => {
  const spec = buildFoundryPipelineSpec({
    builds: [readyBuild],
    feedback: [{ id: "f1" }, { id: "f2" }],
  });
  assert.equal(spec.nodes.length, 9);
  assert.equal(spec.edges.length, 8);
  assert.equal(spec.nodes[0].type, "start");
  assert.equal(spec.nodes[5].type, "approval");
  assert.equal(spec.nodes[8].type, "end");
  assert.equal(spec.nodes[6].label, "Deploy · done");
  assert.equal(spec.edges[0].source, "stage-spec");
  assert.equal(spec.edges[0].target, "stage-validate");
  assert.equal(spec.nodes[3].config.instructions.includes("QA sandbox"), true);
});

test("pipeline spec with no builds is all pending", () => {
  const spec = buildFoundryPipelineSpec({ builds: [], feedback: [] });
  assert.ok(spec.nodes.every((n) => n.label.endsWith("· pending")));
});
