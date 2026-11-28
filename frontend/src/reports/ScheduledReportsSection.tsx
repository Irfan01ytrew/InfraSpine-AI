import { useCallback, useEffect, useMemo, useState } from "react";
import { Clock } from "lucide-react";
import { api, describeError } from "../api";
import {
  reportsApi,
  type DeliveryStatus,
  type ReportCadence,
  type ReportChannel,
  type ReportDelivery,
  type ReportPreference,
  type ReportSchedule,
  type ReportTemplate,
} from "./api";
import {
  channelLabel,
  deliveryRowLabel,
  deliveryStatusLabel,
  scheduleSummary,
  templateLabel,
  validateScheduleForm,
} from "./scheduledReportsView.mjs";
import { Alert, Badge, Button, Card, CardBody, CardHeader, Field, Input, SelectInput, Skeleton } from "../components/ui";

const TEMPLATES: ReportTemplate[] = ["daily_cash_summary", "weekly_invoices", "overdue_alert"];
const CHANNELS: ReportChannel[] = ["whatsapp", "email", "in_app"];
const DELIVERY_STATUSES: DeliveryStatus[] = ["pending", "sending", "sent", "failed", "retry", "cancelled"];

type FormState = {
  template: ReportTemplate;
  cadence: ReportCadence;
  time_of_day: string;
  timezone: string;
  weekday: number;
  channels: ReportChannel[];
  recipients: string[];
  quiet_start: string;
  quiet_end: string;
  overdue_days: number;
  enabled: boolean;
};

const DEFAULT_FORM: FormState = {
  template: "daily_cash_summary",
  cadence: "daily",
  time_of_day: "08:00",
  timezone: "Asia/Karachi",
  weekday: 0,
  channels: ["in_app"],
  recipients: [],
  quiet_start: "22:00",
  quiet_end: "07:00",
  overdue_days: 7,
  enabled: true,
};

function toggle<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
}

export function ScheduledReportsSection() {
  const [schedules, setSchedules] = useState<ReportSchedule[]>([]);
  const [deliveries, setDeliveries] = useState<ReportDelivery[]>([]);
  const [preferences, setPreferences] = useState<ReportPreference[]>([]);
  const [users, setUsers] = useState<{ id: string; email: string; is_active: boolean }[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(DEFAULT_FORM);
  const [formErrors, setFormErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<DeliveryStatus | "">("");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [prefDrafts, setPrefDrafts] = useState<Record<string, {
    channels: ReportChannel[]; cadence: ReportCadence; enabled: boolean; quiet_start: string; quiet_end: string;
  }>>({});

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [scheduleData, deliveryData, preferenceData, userData] = await Promise.all([
        reportsApi.schedules(),
        reportsApi.deliveries(),
        reportsApi.preferences(),
        api.adminUsers(),
      ]);
      setSchedules(scheduleData.schedules);
      setDeliveries(deliveryData.deliveries);
      setPreferences(preferenceData.preferences);
      setUsers(userData.users);
      const drafts: typeof prefDrafts = {};
      for (const template of TEMPLATES) {
        const existing = preferenceData.preferences.find((pref) => pref.template === template);
        drafts[template] = {
          channels: existing?.channels ?? ["in_app"],
          cadence: existing?.cadence ?? "daily",
          enabled: existing?.enabled ?? true,
          quiet_start: existing?.quiet_start ?? "",
          quiet_end: existing?.quiet_end ?? "",
        };
      }
      setPrefDrafts(drafts);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const filteredDeliveries = useMemo(
    () => (statusFilter ? deliveries.filter((d) => d.status === statusFilter) : deliveries).slice(0, 50),
    [deliveries, statusFilter],
  );

  async function createSchedule(event: React.FormEvent) {
    event.preventDefault();
    const errors = validateScheduleForm(form);
    setFormErrors(errors);
    if (Object.keys(errors).length > 0) return;
    setSaving(true);
    setNotice(null);
    try {
      await reportsApi.createSchedule({ ...form, time_of_day: form.time_of_day.slice(0, 5) });
      setNotice("Schedule created.");
      setForm(DEFAULT_FORM);
      await load();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  async function toggleSchedule(schedule: ReportSchedule) {
    try {
      await reportsApi.updateSchedule(schedule.id, { enabled: !schedule.enabled });
      await load();
    } catch (err) {
      setError(describeError(err));
    }
  }

  async function deleteSchedule(id: string) {
    try {
      await reportsApi.deleteSchedule(id);
      setConfirmDelete(null);
      await load();
    } catch (err) {
      setError(describeError(err));
    }
  }

  async function savePreference(template: ReportTemplate) {
    const draft = prefDrafts[template];
    if (!draft) return;
    setSaving(true);
    try {
      await reportsApi.upsertPreference(template, {
        template,
        channels: draft.channels,
        cadence: draft.cadence,
        enabled: draft.enabled,
        quiet_start: draft.quiet_start || null,
        quiet_end: draft.quiet_end || null,
      });
      setNotice(`${templateLabel(template)} preferences saved.`);
      await load();
    } catch (err) {
      setError(describeError(err));
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <Card>
        <CardHeader title="Scheduled reports" icon={<Clock size={18} />} description="Loading schedules…" />
        <CardBody><Skeleton className="h-24 w-full" /></CardBody>
      </Card>
    );
  }

  return (
    <div className="grid gap-4">
      {error && <Alert tone="danger" title="Could not load scheduled reports">{error}</Alert>}
      {notice && <Alert tone="success">{notice}</Alert>}

      <Card>
        <CardHeader
          title="Scheduled reports"
          icon={<Clock size={18} />}
          description="Daily cash summaries, weekly invoices and overdue alerts, sent on WhatsApp, email or in-app. Reports only include rows the recipient is allowed to read, and a report is never sent twice for the same schedule and period."
        />
        <CardBody className="grid gap-4">
          {schedules.length === 0 && (
            <p className="text-sm text-muted">No schedules yet. Create the first one below.</p>
          )}
          {schedules.map((schedule) => (
            <div key={schedule.id} className="flex flex-wrap items-center gap-3 rounded-md border border-border p-3">
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium">{scheduleSummary(schedule)}</p>
                <p className="text-xs text-muted">
                  {schedule.last_sent_at ? `Last sent ${schedule.last_sent_at.slice(0, 16).replace("T", " ")}` : "Never sent yet"}
                  {" · "}Overdue after {schedule.overdue_days} days · Quiet {schedule.quiet_start.slice(0, 5)}–{schedule.quiet_end.slice(0, 5)}
                </p>
              </div>
              <Badge tone={schedule.enabled ? "success" : "neutral"}>{schedule.enabled ? "Enabled" : "Disabled"}</Badge>
              <Button size="sm" onClick={() => void toggleSchedule(schedule)}>
                {schedule.enabled ? "Disable" : "Enable"}
              </Button>
              {confirmDelete === schedule.id ? (
                <Button variant="danger" size="sm" onClick={() => void deleteSchedule(schedule.id)}>Confirm delete</Button>
              ) : (
                <Button size="sm" onClick={() => setConfirmDelete(schedule.id)}>Delete</Button>
              )}
            </div>
          ))}

          <form onSubmit={createSchedule} className="grid gap-3 rounded-md border border-border p-3">
            <h3 className="text-sm font-semibold">New schedule</h3>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Report" error={formErrors.template}>
                <SelectInput value={form.template} onChange={(e) => setForm({ ...form, template: e.target.value as ReportTemplate })}>
                  {TEMPLATES.map((t) => <option key={t} value={t}>{templateLabel(t)}</option>)}
                </SelectInput>
              </Field>
              <Field label="Cadence" error={formErrors.cadence}>
                <SelectInput value={form.cadence} onChange={(e) => setForm({ ...form, cadence: e.target.value as ReportCadence })}>
                  <option value="daily">Daily</option>
                  <option value="weekly">Weekly</option>
                </SelectInput>
              </Field>
              <Field label="Send time" error={formErrors.time_of_day}>
                <Input type="time" value={form.time_of_day} onChange={(e) => setForm({ ...form, time_of_day: e.target.value })} />
              </Field>
              <Field label="Timezone">
                <Input value={form.timezone} onChange={(e) => setForm({ ...form, timezone: e.target.value })} />
              </Field>
              {form.cadence === "weekly" && (
                <Field label="Weekday" error={formErrors.weekday}>
                  <SelectInput value={form.weekday} onChange={(e) => setForm({ ...form, weekday: Number(e.target.value) })}>
                    {["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].map((day, index) => (
                      <option key={day} value={index}>{day}</option>
                    ))}
                  </SelectInput>
                </Field>
              )}
              <Field label="Overdue after (days)" error={formErrors.overdue_days}>
                <Input type="number" min={0} value={form.overdue_days} onChange={(e) => setForm({ ...form, overdue_days: Number(e.target.value) })} />
              </Field>
            </div>
            <Field label="Channels" error={formErrors.channels}>
              <div className="flex flex-wrap gap-4">
                {CHANNELS.map((channel) => (
                  <label key={channel} className="flex items-center gap-2 text-sm">
                    <input type="checkbox" checked={form.channels.includes(channel)} onChange={() => setForm({ ...form, channels: toggle(form.channels, channel) })} />
                    {channelLabel(channel)}
                  </label>
                ))}
              </div>
            </Field>
            <Field label="Recipients" error={formErrors.recipients}>
              <div className="flex flex-wrap gap-4">
                {users.filter((u) => u.is_active).map((user) => (
                  <label key={user.id} className="flex items-center gap-2 text-sm">
                    <input type="checkbox" checked={form.recipients.includes(user.id)} onChange={() => setForm({ ...form, recipients: toggle(form.recipients, user.id) })} />
                    {user.email}
                  </label>
                ))}
              </div>
            </Field>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Quiet hours start" error={formErrors.quiet_hours}>
                <Input type="time" value={form.quiet_start} onChange={(e) => setForm({ ...form, quiet_start: e.target.value })} />
              </Field>
              <Field label="Quiet hours end">
                <Input type="time" value={form.quiet_end} onChange={(e) => setForm({ ...form, quiet_end: e.target.value })} />
              </Field>
            </div>
            <p className="text-xs text-muted">
              WhatsApp delivery needs a connected WhatsApp number and a recipient who already messaged the business;
              email delivery needs SMTP turned on. Until a channel is configured, deliveries wait in the retry queue instead of being dropped.
            </p>
            <div><Button type="submit" disabled={saving}>{saving ? "Creating…" : "Create schedule"}</Button></div>
          </form>
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          title="My report preferences"
          description="Your own channels and quiet hours for each report. These only change what you receive; schedules still decide when reports are built."
        />
        <CardBody className="grid gap-4">
          {TEMPLATES.map((template) => {
            const draft = prefDrafts[template];
            if (!draft) return null;
            return (
              <div key={template} className="grid gap-3 rounded-md border border-border p-3">
                <h3 className="text-sm font-semibold">{templateLabel(template)}</h3>
                <div className="flex flex-wrap gap-4">
                  {CHANNELS.map((channel) => (
                    <label key={channel} className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={draft.channels.includes(channel)}
                        onChange={() => setPrefDrafts({ ...prefDrafts, [template]: { ...draft, channels: toggle(draft.channels, channel) } })}
                      />
                      {channelLabel(channel)}
                    </label>
                  ))}
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={draft.enabled}
                      onChange={() => setPrefDrafts({ ...prefDrafts, [template]: { ...draft, enabled: !draft.enabled } })}
                    />
                    Enabled
                  </label>
                </div>
                <div className="grid gap-3 sm:grid-cols-3">
                  <Field label="Cadence">
                    <SelectInput value={draft.cadence} onChange={(e) => setPrefDrafts({ ...prefDrafts, [template]: { ...draft, cadence: e.target.value as ReportCadence } })}>
                      <option value="daily">Daily</option>
                      <option value="weekly">Weekly</option>
                    </SelectInput>
                  </Field>
                  <Field label="Quiet hours start">
                    <Input type="time" value={draft.quiet_start} onChange={(e) => setPrefDrafts({ ...prefDrafts, [template]: { ...draft, quiet_start: e.target.value } })} />
                  </Field>
                  <Field label="Quiet hours end">
                    <Input type="time" value={draft.quiet_end} onChange={(e) => setPrefDrafts({ ...prefDrafts, [template]: { ...draft, quiet_end: e.target.value } })} />
                  </Field>
                </div>
                <div><Button size="sm" disabled={saving} onClick={() => void savePreference(template)}>Save preferences</Button></div>
              </div>
            );
          })}
          {preferences.length === 0 && (
            <p className="text-xs text-muted">No saved preferences yet — the defaults above apply.</p>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          title="Delivery history"
          description="Latest deliveries across all schedules. A failed delivery is retried with backoff, up to 8 attempts, and never sent twice."
        />
        <CardBody className="grid gap-3">
          <Field label="Status filter">
            <SelectInput value={statusFilter} onChange={(e) => setStatusFilter(e.target.value as DeliveryStatus | "")}>
              <option value="">All statuses</option>
              {DELIVERY_STATUSES.map((status) => <option key={status} value={status}>{deliveryStatusLabel(status)}</option>)}
            </SelectInput>
          </Field>
          {filteredDeliveries.length === 0 && <p className="text-sm text-muted">No deliveries match.</p>}
          {filteredDeliveries.map((delivery) => (
            <div key={delivery.id} className="flex flex-wrap items-center gap-3 rounded-md border border-border p-3">
              <div className="min-w-0 flex-1">
                <p className="text-sm">{deliveryRowLabel(delivery)}</p>
                <p className="text-xs text-muted">
                  Period {delivery.period_start.slice(0, 10)} · {delivery.attempts} attempt(s)
                  {delivery.last_error_code ? ` · Last error: ${delivery.last_error_code}` : ""}
                  {delivery.provider_ref ? ` · Ref: ${delivery.provider_ref}` : ""}
                </p>
              </div>
              <Badge tone={delivery.status === "sent" ? "success" : delivery.status === "failed" ? "danger" : "neutral"}>
                {deliveryStatusLabel(delivery.status)}
              </Badge>
            </div>
          ))}
        </CardBody>
      </Card>
    </div>
  );
}
