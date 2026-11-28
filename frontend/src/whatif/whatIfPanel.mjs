/** Pure helpers for the what-if twin panel (Step AL slice 6). No network, no DOM. */

/** Seed choices for the scenario form: visible entities only, sorted by name. */
export function seedOptions(graph) {
  if (!graph || !Array.isArray(graph.entities)) return [];
  return graph.entities
    .filter((entity) => entity.record_type === "entity")
    .map((entity) => ({ id: entity.id, name: entity.name || entity.id, kind: entity.kind || "" }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

/** One-line summary of a simulation result, in plain language. */
export function summarizeSimulation(result) {
  const affectedCount = Array.isArray(result.affected) ? result.affected.length : 0;
  const failedCount = Array.isArray(result.failed) ? result.failed.length : 0;
  const degradedCount = Array.isArray(result.degraded) ? result.degraded.length : 0;
  const waves = result.recovery_plan && Array.isArray(result.recovery_plan.waves)
    ? result.recovery_plan.waves : [];
  const stepCount = waves.reduce((total, wave) => total + wave.length, 0);
  const blockedCount = result.recovery_plan && Array.isArray(result.recovery_plan.blocked_by_dependency_cycle)
    ? result.recovery_plan.blocked_by_dependency_cycle.length : 0;
  const totals = (result.money_at_risk && result.money_at_risk.totals) || {};
  const parts = Object.entries(totals).map(([currency, amount]) => `${currency} ${Number(amount).toLocaleString("en-PK")}`);
  const moneyText = parts.length
    ? `${parts.join(", ")} at risk across ${result.money_at_risk.batch_count || 0} batch(es)`
    : "No money at risk";
  return { affectedCount, failedCount, degradedCount, waveCount: waves.length, stepCount, blockedCount, moneyText };
}

/** Progress of a rehearsal record: counts + a plain-language headline. */
export function rehearsalProgress(rehearsal) {
  const steps = Array.isArray(rehearsal.steps) ? rehearsal.steps : [];
  const total = steps.length;
  const done = steps.filter((step) => step.status === "done").length;
  const skipped = steps.filter((step) => step.status === "skipped").length;
  const pending = total - done - skipped;
  const headline = total === 0
    ? "This rehearsal has no steps."
    : pending === 0
      ? `All ${total} step(s) practised (${done} done, ${skipped} skipped).`
      : `${done} of ${total} step(s) done, ${skipped} skipped, ${pending} still to practise.`;
  return { total, done, skipped, pending, allDone: total > 0 && pending === 0, headline };
}

/** The next step still waiting to be practised, or null when none remain. */
export function nextPendingStep(rehearsal) {
  const steps = Array.isArray(rehearsal.steps) ? rehearsal.steps : [];
  return steps.find((step) => step.status === "pending") || null;
}

/** One plain-language sentence for a drill comparison. */
export function comparisonHeadline(comparison) {
  const summary = comparison.summary || {};
  const coverage = Math.round((summary.coverage || 0) * 100);
  const verified = Math.round((summary.drill_verified_rate || 0) * 100);
  const missingCount = summary.missing_count ?? (Array.isArray(comparison.missing) ? comparison.missing.length : 0);
  const simulatedCount = summary.simulated_count ?? 0;
  if (missingCount === 0 && coverage === 100) {
    return `The drill covered all ${simulatedCount} predicted step(s); ${verified}% of drill steps were verified.`;
  }
  return `The drill covered ${coverage}% of predicted steps (${missingCount} predicted step(s) missing); ${verified}% of drill steps were verified.`;
}
