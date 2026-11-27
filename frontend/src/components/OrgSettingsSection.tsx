import { useEffect, useState } from "react";
import { Clock } from "lucide-react";
import { api, describeError } from "../api";
import { Spinner } from "../icons";
import { formatOrgDay, zoneOptions } from "../orgTimeZone.mjs";
import type { OrgSettings } from "../types";
import { Alert, Button, Card, CardBody, CardHeader, Field, SelectInput } from "./ui";

/** Organization time zone: what "today" means for delegations, budgets and due dates. */
export function OrgSettingsSection() {
  const [settings, setSettings] = useState<OrgSettings | null>(null);
  const [zones, setZones] = useState<string[]>([]);
  const [choice, setChoice] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let mounted = true;
    void api.orgSettings().then((value) => {
      if (!mounted) return;
      setSettings(value);
      setChoice(value.time_zone);
    }).catch((reason: unknown) => {
      if (mounted) setError(describeError(reason));
    }).finally(() => {
      if (mounted) setLoading(false);
    });
    // The zone list is optional: without it the current and common zones are still offered.
    void api.orgTimeZones().then((value) => { if (mounted) setZones(value.time_zones); }).catch(() => undefined);
    return () => { mounted = false; };
  }, []);

  async function save() {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const value = await api.saveOrgTimeZone(choice);
      setSettings(value);
      setChoice(value.time_zone);
      setSaved(true);
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setSaving(false);
    }
  }

  const options = zoneOptions(zones, settings?.time_zone ?? choice);
  return (
    <Card aria-labelledby="org-settings-title">
      <CardHeader
        icon={<Clock />}
        title={<span id="org-settings-title">Organization time zone</span>}
        description="Times are stored in UTC. Business days (delegations, budgets, due dates) follow this zone."
      />
      <CardBody className="grid gap-4">
        {error && <Alert tone="danger">{error}</Alert>}
        {saved && <Alert tone="success">Time zone saved.</Alert>}
        {loading ? (
          <div className="loading"><Spinner size={22} /></div>
        ) : settings ? (
          <>
            <p className="text-sm text-muted">
              Today in the organization: <strong className="text-foreground">{formatOrgDay(settings.today)}</strong>, {settings.local_time} ({settings.time_zone})
            </p>
            <Field label="Time zone" htmlFor="org-time-zone" hint={`Default: ${settings.default_time_zone}`}>
              <SelectInput id="org-time-zone" aria-label="Organization time zone" value={choice} onChange={(event) => { setChoice(event.target.value); setSaved(false); }}>
                {options.map((zone) => <option key={zone} value={zone}>{zone}</option>)}
              </SelectInput>
            </Field>
            <div>
              <Button variant="primary" size="sm" loading={saving} disabled={saving || !choice || choice === settings.time_zone} onClick={() => void save()}>
                Save time zone
              </Button>
            </div>
          </>
        ) : null}
      </CardBody>
    </Card>
  );
}
