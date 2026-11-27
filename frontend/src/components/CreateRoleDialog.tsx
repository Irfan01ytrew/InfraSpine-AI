import { useEffect, useState } from "react";
import { Plus } from "lucide-react";
import { api, describeError } from "../api";
import type { PermissionCatalogEntry, RoleTemplate, RoleTemplatePermission } from "../types";
import { useFeedback } from "./Feedback";
import { CATEGORY_PERMISSIONS, permissionLabel } from "./permissionLabels";
import { Chips } from "./RoleTemplatesSection";
import { Badge, Button, Dialog, Field, Input, Select, Tabs } from "./ui";

const categories = ["policy", "financial", "legal", "client", "operational", "general"] as const;

type LibraryRole = { key: string; name: string; base_role: "employee" | "manager"; description: string; permissions: RoleTemplatePermission[] };

/** Create a custom role without leaving the current form (used by the invite role pickers). */
export function CreateRoleDialog({ open, onOpenChange, existing, permissions, onCreated }: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  existing: RoleTemplate[];
  permissions: PermissionCatalogEntry["permission"][];
  onCreated: (role: RoleTemplate) => void;
}) {
  const { toast } = useFeedback();
  const [mode, setMode] = useState<"library" | "custom">("library");
  const [library, setLibrary] = useState<LibraryRole[]>([]);
  const [search, setSearch] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [baseRole, setBaseRole] = useState<"employee" | "manager">("employee");
  const [selectedPermissions, setSelectedPermissions] = useState<RoleTemplatePermission["permission"][]>([]);
  const [selectedCategories, setSelectedCategories] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    api.roleLibrary().then((rows) => setLibrary(rows.roles)).catch(() => setLibrary([]));
  }, [open]);

  const existingNames = new Set(existing.map((item) => item.name.toLowerCase()));
  const query = search.trim().toLowerCase();
  const available = library.filter((preset) => !existingNames.has(preset.name.toLowerCase())
    && (!query || `${preset.name} ${preset.description}`.toLowerCase().includes(query)));
  const needsCategories = selectedPermissions.some((permission) => CATEGORY_PERMISSIONS.has(permission));
  const nameTaken = existingNames.has(name.trim().toLowerCase());
  const canCreate = name.trim().length >= 2 && !nameTaken && selectedPermissions.length > 0 && (!needsCategories || selectedCategories.length > 0);

  async function save(input: Parameters<typeof api.createRoleTemplate>[0]) {
    setBusy(true);
    try {
      const created = await api.createRoleTemplate(input);
      toast("success", `${created.name} created`, "It is now selected for this invitation.");
      setName(""); setDescription(""); setSelectedPermissions([]); setSelectedCategories([]); setSearch("");
      onCreated(created);
      onOpenChange(false);
    } catch (err) { toast("error", "Could not create the role", describeError(err)); }
    finally { setBusy(false); }
  }

  function createCustom() {
    if (!canCreate) return;
    void save({
      name: name.trim(),
      description: description.trim(),
      base_role: baseRole,
      permissions: selectedPermissions.map((permission) => ({
        permission,
        categories: (CATEGORY_PERMISSIONS.has(permission) ? selectedCategories : []) as RoleTemplatePermission["categories"],
      })),
    });
  }

  const rightsText = (specs: RoleTemplatePermission[]) => specs.map((spec) => spec.categories.length ? `${permissionLabel(spec.permission)} (${spec.categories.join(", ")})` : permissionLabel(spec.permission));

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      className="max-w-2xl"
      title="Create a new role"
      description="Pick a ready-made role or build your own. The role adds rights on top of an Employee or Manager account, only in the departments you invite them to."
      footer={mode === "custom" ? <><Button onClick={() => onOpenChange(false)}>Cancel</Button><Button variant="primary" loading={busy} disabled={busy || !canCreate} onClick={createCustom}>{!busy && <Plus />} Create and select</Button></> : undefined}
    >
      <div className="grid gap-4">
        <Tabs value={mode} onValueChange={(value) => setMode(value as "library" | "custom")} items={[{ value: "library", label: "Ready-made", count: available.length }, { value: "custom", label: "Build your own" }]} />
        {mode === "library" ? (
          <>
            <Input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search roles, e.g. HR, finance, quality" aria-label="Search ready-made roles" />
            {available.length === 0 ? (
              <p className="text-xs text-muted">{query ? "No ready-made role matches. Try “Build your own”." : "Every ready-made role is already added. Use “Build your own” for any other name."}</p>
            ) : (
              <div className="grid gap-2 sm:grid-cols-2">
                {available.map((preset) => (
                  <div key={preset.key} className="flex flex-col gap-2 rounded-lg border border-border p-3">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0"><p className="text-[13px] font-semibold text-foreground">{preset.name}</p><p className="text-xs text-muted">{preset.description}</p></div>
                      <Badge>{preset.base_role === "manager" ? "Manager" : "Employee"}</Badge>
                    </div>
                    <div className="flex flex-wrap gap-1">{rightsText(preset.permissions).map((text) => <Badge key={text} tone="primary">{text}</Badge>)}</div>
                    <Button size="sm" className="mt-auto self-start" disabled={busy} onClick={() => void save({ name: preset.name, description: preset.description, base_role: preset.base_role, permissions: preset.permissions })}><Plus /> Use this role</Button>
                  </div>
                ))}
              </div>
            )}
          </>
        ) : (
          <div className="grid gap-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Role name" error={nameTaken ? "A role with this name already exists — pick it from the list instead." : undefined}><Input autoFocus maxLength={80} value={name} onChange={(event) => setName(event.target.value)} placeholder="Any name, e.g. Site Engineer" /></Field>
              <Field label="Account type"><Select aria-label="Account type" value={baseRole} onValueChange={(value) => setBaseRole(value as "employee" | "manager")} options={[{ value: "employee", label: "Employee" }, { value: "manager", label: "Manager" }]} /></Field>
            </div>
            <Field label="Description (optional)"><Input maxLength={500} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="What this role is for" /></Field>
            <Field label="What can this role do?" hint="Choose at least one right.">
              <Chips options={permissions.map((item) => ({ value: item, label: permissionLabel(item) }))} selected={selectedPermissions} onToggle={(value) => setSelectedPermissions((current) => current.includes(value as RoleTemplatePermission["permission"]) ? current.filter((item) => item !== value) : [...current, value as RoleTemplatePermission["permission"]])} />
            </Field>
            {needsCategories && <Field label="Knowledge categories (for the knowledge rights)"><Chips options={categories.map((item) => ({ value: item, label: item[0].toUpperCase() + item.slice(1) }))} selected={selectedCategories} onToggle={(value) => setSelectedCategories((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value])} /></Field>}
          </div>
        )}
      </div>
    </Dialog>
  );
}
