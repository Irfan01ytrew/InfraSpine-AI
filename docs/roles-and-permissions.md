# Roles and scoped permissions

InfraSpine keeps the built-in **Admin**, **Manager**, and **Employee** roles. Admin is protected and cannot be created as a custom role. Managers and Employees can receive additional, department-scoped governance permissions.

## Create and assign a template

1. Sign in as an Admin and open **People & access**.
2. Under **Custom permission templates**, enter a name, choose a Manager or Employee base role, and select one or more knowledge categories.
3. Select the supported action: `knowledge.review` or `knowledge.publish`. The editor does not offer permissions without an authorization consumer.
4. Choose an active person with the matching base role, select one or more departments already assigned to that person, and assign the template.
5. Revoke an assignment from the **Active custom role assignments** table when it is no longer needed.

The grant is checked by the existing PostgreSQL-backed approval service. An Employee assigned review may review only matching categories and department scope. A Manager assigned publish may publish only after a review by a different person. Authors cannot approve their own submissions. The grant does not bypass document ACLs or grant access to restricted source documents.

Template definitions are immutable. To change a definition, create a new named template and explicitly assign it; the current release does not create numbered successor versions. Deactivating a template blocks new assignments but leaves existing grants in force until each assignment is revoked. Revocation takes effect immediately and retains the assignment, grant and audit history.

Assignments are revoked automatically when the account is deactivated, its built-in role or department set changes, or an Admin removes the account from a department included in an assignment. The assignment path locks the user row so a concurrent role/department update cannot leave a newly issued stale grant.

## Current boundaries

- Templates cover only the existing knowledge review and publish actions. They do not define arbitrary API/action rights, employee-specific UI visibility, time-limited delegation, or approval for sensitive permission changes.
- The legacy `data.steward` grant type remains in the general access schema for compatibility, but role templates do not issue it because no authorization route currently consumes it.
- Invitation links are returned once and are not emailed. Send them through an approved company channel.
- Adding a member to a department does not issue governance permission. An Admin must assign a suitable template separately.

## Administrative API

All routes below are Admin-only and require the application's normal authenticated session and CSRF header:

- `GET` / `POST` `/api/admin/role-templates`
- `PATCH` `/api/admin/role-templates/{template_id}/active`
- `POST` `/api/admin/role-templates/{template_id}/assignments`
- `GET` `/api/admin/role-template-assignments`
- `POST` `/api/admin/role-template-assignments/{assignment_id}/revoke`

Manager and Employee requests to these routes are denied. Revoked assignment history is preserved in PostgreSQL and the audit log; the current API list shows active assignments only.
