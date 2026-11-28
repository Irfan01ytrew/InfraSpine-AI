import { useEffect, useMemo, useState } from "react";
import { Route } from "lucide-react";
import { api, describeError } from "../api";
import { Spinner } from "../icons";
import type { ModelOption, RoutingPolicy, RoutingPolicySave, RoutingReport } from "../types";
import { Alert, Button, Card, CardBody, CardHeader, Field, Input, SelectInput } from "./ui";

const TASK_LABELS: Record<string, string> = {
  simple: "Simple",
  standard: "Standard",
  complex: "Complex",
  vision: "Vision",
  grounded_research: "Grounded research",
};

const POLICY_LABELS: Record<string, string> = {
  cheapest: "Cheapest capable",
  fastest: "Fastest",
  balanced: "Balanced (meets the task's capability tier)",
};

const TIER_LABELS: Record<string, string> = {
  "": "Heuristic (from the model name)",
  "1": "1 — Fast / cheap",
  "2": "2 — Standard",
  "3": "3 — Flagship reasoning",
};

const MONEY_RE = /^\d+(\.\d{1,2})?$/;

interface Draft {
  default_policy: string;
  task_class_policies: Record<string, string>;
  capability_tiers: Record<string, string>;
  monthly_budget_usd: string;
  department_budgets: Record<string, string>;
}

function draftFrom(policy: RoutingPolicy): Draft {
  return {
    default_policy: policy.default_policy,
    task_class_policies: Object.fromEntries(
      policy.task_classes.map((taskClass) => [taskClass, policy.task_class_policies[taskClass] ?? ""]),
    ),
    capability_tiers: {},
    monthly_budget_usd: policy.monthly_budget_usd ?? "",
    department_budgets: Object.fromEntries(
      policy.departments.map((department) => [department.id, policy.department_budgets[department.id] ?? ""]),
    ),
  };
}

function money(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const amount = Number(value);
  return Number.isFinite(amount) ? `$${amount.toFixed(2)}` : value;
}

/** Smart model router: which model answers, per task class, and the monthly budget. */
export function RoutingPolicySection() {
  const [policy, setPolicy] = useState<RoutingPolicy | null>(null);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [report, setReport] = useState<RoutingReport | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let mounted = true;
    void api
      .routingPolicy()
      .then((value) => {
        if (!mounted) return;
        setPolicy(value);
        setDraft(draftFrom(value));
      })
      .catch((reason: unknown) => {
        if (mounted) setError(describeError(reason));
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    void api.chatModels().then((value) => {
      if (mounted) setModels(value.options);
    }).catch(() => undefined);
    void api.routingReport().then((value) => {
      if (mounted) setReport(value);
    }).catch(() => undefined);
    return () => {
      mounted = false;
    };
  }, []);

  const dirty = useMemo(() => {
    if (!policy || !draft) return false;
    return JSON.stringify(draft) !== JSON.stringify(draftFrom(policy));
  }, [policy, draft]);

  function update(patch: Partial<Draft>) {
    setDraft((current) => (current ? { ...current, ...patch } : current));
    setSaved(false);
  }

  function validate(): string | null {
    if (!draft) return "Nothing to save yet.";
    if (draft.monthly_budget_usd !== "" && !MONEY_RE.test(draft.monthly_budget_usd))
      return "Monthly budget must be a non-negative amount in USD, e.g. 25.00.";
    for (const [departmentId, value] of Object.entries(draft.department_budgets)) {
      if (value !== "" && !MONEY_RE.test(value)) {
        const name = policy?.departments.find((d) => d.id === departmentId)?.name ?? departmentId;
        return `Budget for ${name} must be a non-negative amount in USD, e.g. 25.00.`;
      }
    }
    return null;
  }

  async function save() {
    if (!draft) return;
    const problem = validate();
    if (problem) {
      setError(problem);
      return;
    }
    setSaving(true);
    setError(null);
    setSaved(false);
    const body: RoutingPolicySave = {
      default_policy: draft.default_policy,
      task_class_policies: Object.fromEntries(
        Object.entries(draft.task_class_policies).filter(([, value]) => value !== ""),
      ),
      capability_tiers: Object.fromEntries(
        Object.entries(draft.capability_tiers)
          .filter(([, value]) => value !== "")
          .map(([key, value]) => [key, Number(value)]),
      ),
      monthly_budget_usd: draft.monthly_budget_usd === "" ? null : draft.monthly_budget_usd,
      department_budgets: Object.fromEntries(
        Object.entries(draft.department_budgets).filter(([, value]) => value !== ""),
      ),
    };
    try {
      const value = await api.saveRoutingPolicy(body);
      setPolicy(value);
      setDraft(draftFrom(value));
      setSaved(true);
      const fresh = await api.routingReport().catch(() => null);
      if (fresh) setReport(fresh);
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card aria-labelledby="routing-policy-title">
      <CardHeader
        icon={<Route />}
        title={<span id="routing-policy-title">Smart model router</span>}
        description="Which model answers each kind of question, and how much routed calls may spend each month. Changes apply to the next call — no restart is needed."
      />
      <CardBody className="grid gap-4">
        {error && <Alert tone="danger">{error}</Alert>}
        {saved && <Alert tone="success">Routing policy saved.</Alert>}
        {loading ? (
          <div className="loading">
            <Spinner size={22} />
          </div>
        ) : policy && draft ? (
          <>
            <p className="text-sm text-muted">
              Routed spend this month: <strong className="text-foreground">{money(policy.spent_this_month_usd)}</strong>
              {policy.monthly_budget_usd !== null && (
                <>
                  {" "}of a {money(policy.monthly_budget_usd)} budget
                  {report?.remaining_usd !== null && report?.remaining_usd !== undefined && (
                    <> — {money(report.remaining_usd)} left</>
                  )}
                </>
              )}
              {policy.monthly_budget_usd === null && " — no monthly budget is set, so the guard never refuses."}
            </p>
            <Field
              label="Default policy"
              htmlFor="routing-default-policy"
              hint="How the router ranks the models that can do the job."
            >
              <SelectInput
                id="routing-default-policy"
                value={draft.default_policy}
                onChange={(event) => update({ default_policy: event.target.value })}
              >
                {policy.policies.map((name) => (
                  <option key={name} value={name}>
                    {POLICY_LABELS[name] ?? name}
                  </option>
                ))}
              </SelectInput>
            </Field>
            <fieldset className="grid gap-3">
              <legend className="text-sm font-medium">Policy per task class</legend>
              {policy.task_classes.map((taskClass) => (
                <Field
                  key={taskClass}
                  label={TASK_LABELS[taskClass] ?? taskClass}
                  htmlFor={`routing-policy-${taskClass}`}
                >
                  <SelectInput
                    id={`routing-policy-${taskClass}`}
                    value={draft.task_class_policies[taskClass] ?? ""}
                    onChange={(event) =>
                      update({
                        task_class_policies: { ...draft.task_class_policies, [taskClass]: event.target.value },
                      })
                    }
                  >
                    <option value="">Use the default policy</option>
                    {policy.policies.map((name) => (
                      <option key={name} value={name}>
                        {POLICY_LABELS[name] ?? name}
                      </option>
                    ))}
                  </SelectInput>
                </Field>
              ))}
            </fieldset>
            <fieldset className="grid gap-3">
              <legend className="text-sm font-medium">Capability tier per model</legend>
              <p className="text-xs text-muted">
                The reasoning tier the router assumes for each configured model. Leave it on “Heuristic” unless a
                model is really stronger or weaker than its name suggests — the Balanced policy only picks models
                whose tier meets the task.
              </p>
              {models.map((option) => {
                const key = `${option.provider}:${option.model}`;
                return (
                  <Field
                    key={key}
                    label={`${option.provider_label} · ${option.model_label}`}
                    htmlFor={`routing-tier-${option.provider}-${option.model}`}
                  >
                    <SelectInput
                      id={`routing-tier-${option.provider}-${option.model}`}
                      value={draft.capability_tiers[key] ?? ""}
                      onChange={(event) =>
                        update({
                          capability_tiers: { ...draft.capability_tiers, [key]: event.target.value },
                        })
                      }
                    >
                      {Object.entries(TIER_LABELS).map(([value, label]) => (
                        <option key={value} value={value}>
                          {label}
                        </option>
                      ))}
                    </SelectInput>
                  </Field>
                );
              })}
              {models.length === 0 && (
                <p className="text-xs text-muted">No provider models are configured yet.</p>
              )}
            </fieldset>
            <fieldset className="grid gap-3">
              <legend className="text-sm font-medium">Monthly budget (USD)</legend>
              <Field
                label="Organization budget"
                htmlFor="routing-budget"
                hint="Blank means no budget is enforced. A complex task over budget is refused; other tasks are downgraded to the cheapest capable model."
              >
                <Input
                  id="routing-budget"
                  type="number"
                  min={0}
                  step="0.01"
                  inputMode="decimal"
                  placeholder="No budget"
                  value={draft.monthly_budget_usd}
                  onChange={(event) => update({ monthly_budget_usd: event.target.value })}
                />
              </Field>
              {policy.departments.map((department) => (
                <Field
                  key={department.id}
                  label={`Budget for ${department.name}`}
                  htmlFor={`routing-budget-${department.id}`}
                  hint="Blank means this department uses the organization budget."
                >
                  <Input
                    id={`routing-budget-${department.id}`}
                    type="number"
                    min={0}
                    step="0.01"
                    inputMode="decimal"
                    placeholder="Organization budget"
                    value={draft.department_budgets[department.id] ?? ""}
                    onChange={(event) =>
                      update({
                        department_budgets: { ...draft.department_budgets, [department.id]: event.target.value },
                      })
                    }
                  />
                </Field>
              ))}
              {policy.departments.length === 0 && (
                <p className="text-xs text-muted">No departments exist yet.</p>
              )}
            </fieldset>
            {report && report.by_task_class.length > 0 && (
              <div>
                <h3 className="mb-2 text-sm font-medium">Spend by task class (last {report.days} days)</h3>
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-left text-muted">
                      <th className="py-1 pr-2">Task class</th>
                      <th className="py-1 pr-2">Calls</th>
                      <th className="py-1 pr-2">Cost</th>
                      <th className="py-1 pr-2">Avg latency</th>
                      <th className="py-1">Refused</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.by_task_class.map((row) => (
                      <tr key={row.task_class} className="border-t border-border">
                        <td className="py-1 pr-2">{TASK_LABELS[row.task_class] ?? row.task_class}</td>
                        <td className="py-1 pr-2">{row.calls}</td>
                        <td className="py-1 pr-2">{money(row.total_cost_usd)}</td>
                        <td className="py-1 pr-2">
                          {row.avg_latency_ms === null ? "—" : `${Math.round(row.avg_latency_ms)} ms`}
                        </td>
                        <td className="py-1">{row.refused_calls}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div>
              <Button variant="primary" size="sm" loading={saving} disabled={saving || !dirty} onClick={() => void save()}>
                Save routing policy
              </Button>
            </div>
          </>
        ) : null}
      </CardBody>
    </Card>
  );
}
