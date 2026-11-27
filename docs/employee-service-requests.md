# Employee service requests

The **Help & requests** workspace lets employees submit IT, access, facilities, HR, and other internal requests. Requests are handled by people; InfraSpine does not send their text to an AI provider.

## Visibility

- A requester can see and update their own request history.
- A manager can see and triage non-HR requests for departments they manage.
- HR requests are visible only to the requester and an administrator. Department managers receive the same not-found response as other out-of-scope users.
- Administrators can review and assign all requests. HR requests can only be assigned to another active administrator.
- Department assignment is validated when a request is submitted and again when it is assigned. A stale or out-of-department assignee is rejected.

The event history is append-only. Subject and description text are not copied into the general security audit log. Avoid entering passwords, API keys, or other secrets in request fields or comments.

## Response targets and escalation

The initial response target uses elapsed UTC time from submission: urgent requests have 1 hour, high 4 hours, normal 24 hours, and low 72 hours. Moving a request out of `submitted` records the first response time. If it is still unanswered at its deadline, the worker adds one immutable escalation event and writes fixed-type in-app alerts in the same database transaction. A uniqueness key and row lock make retries and concurrent workers idempotent. No request text is copied into an alert, and this path does not send email or call an external notification provider.

The requester receives an alert. A non-HR request goes to its active department managers (and an active assignee); when there is no active manager, active administrators are the fallback. HR alerts go only to the requester and active administrators. The notification API rechecks current request access on every list/read operation, so department access revocation hides earlier alerts. The alert appears in **Help & requests**.

## Status flow

New requests start as `submitted`. Authorized managers or administrators may acknowledge, start work, ask the requester for more information, resolve, or reject a request. A resolution or rejection requires an explanatory note. Requesters can cancel a new or acknowledged request. Comments and status changes are retained in the request timeline.

## API

- `GET /api/service-requests` lists only requests the signed-in user may read.
- `POST /api/service-requests` submits a request for an assigned department or as a private no-department request.
- `GET /api/service-requests/{id}` returns one authorized request and its timeline.
- `POST /api/service-requests/{id}/comments` adds a timeline entry.
- `POST /api/service-requests/{id}/triage` updates status and an optional validated assignee; Admin or an assigned department manager only.
- `POST /api/service-requests/{id}/cancel` cancels the requester's new or acknowledged request.

Migration `0027` adds the request and immutable timeline tables; migration `0028` adds adaptive training practice; migration `0029` adds response targets and idempotent in-app escalation alerts. Apply migrations through the normal backup-first upgrade process; verify them on a disposable clone before upgrading a customer installation. The SLA scheduler runs with `python -m app.worker` and is included in `python -m app.worker --once`.
