"""Code-owned connector contracts; users cannot install executable adapters."""
CATALOG = {
    "rest": {"name": "REST / business API", "base_url": "", "operations": ["verify", "sync", "rest.write"], "scopes": []},
    "graphql": {"name": "GraphQL", "base_url": "", "operations": ["verify", "sync"], "scopes": []},
    "soap": {"name": "SOAP service", "base_url": "", "operations": ["soap.call"], "scopes": []},
    "microsoft365": {"name": "Microsoft 365", "base_url": "https://graph.microsoft.com/v1.0", "operations": ["verify", "sync", "email.send", "calendar.create"], "scopes": ["User.Read", "Mail.Read", "Mail.Send", "Calendars.ReadWrite", "Files.Read", "offline_access"], "verify_path": "/me", "sync_path": "/me/messages", "records_path": "value"},
    "gmail": {"name": "Gmail", "base_url": "https://gmail.googleapis.com/gmail/v1", "operations": ["verify", "sync", "email.send"], "scopes": ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send"], "verify_path": "/users/me/profile", "sync_path": "/users/me/messages", "records_path": "messages"},
    "google_calendar": {"name": "Google Calendar", "base_url": "https://www.googleapis.com/calendar/v3", "operations": ["verify", "sync", "calendar.create"], "scopes": ["https://www.googleapis.com/auth/calendar.events"], "verify_path": "/calendars/primary", "sync_path": "/calendars/primary/events", "records_path": "items"},
    "google_drive": {"name": "Google Drive", "base_url": "https://www.googleapis.com/drive/v3", "operations": ["verify", "sync"], "scopes": ["https://www.googleapis.com/auth/drive.readonly"], "verify_path": "/about", "sync_path": "/files", "records_path": "files"},
    "slack": {"name": "Slack", "base_url": "https://slack.com/api", "operations": ["verify", "slack.send"], "scopes": ["chat:write"], "verify_path": "/auth.test"},
}
WRITE_SCOPES = {("microsoft365", "email.send"): "Mail.Send", ("microsoft365", "calendar.create"): "Calendars.ReadWrite",
                ("gmail", "email.send"): "https://www.googleapis.com/auth/gmail.send",
                ("google_calendar", "calendar.create"): "https://www.googleapis.com/auth/calendar.events",
                ("slack", "slack.send"): "chat:write"}
READ_OPERATIONS = frozenset({"verify", "sync"})
