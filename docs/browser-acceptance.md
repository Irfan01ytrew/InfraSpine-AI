# Browser acceptance checks

The dependency-free smoke check drives installed Chrome, Chromium, or Edge through the local DevTools protocol. It creates a temporary browser profile, opens the existing frontend, and checks the actual rendered sign-in form. It never starts an API/worker, sends credentials, or changes application data.

From the repository root, start the frontend using the project's normal development process, then run:

```powershell
npm.cmd --prefix frontend run test:browser -- http://localhost:5173/
```

The default address is `http://localhost:5173/` for Vite development only; it returns connection refused when the dev server is not running. The current synthetic Docker QA candidate serves the built UI from `http://localhost:18081/`. Use the exact published loopback port reported by `docker ps`; an old port is not proof that a service is still running.

Set `INFRASPINE_BROWSER_PATH` to an installed browser executable when it is outside the standard Windows/Linux paths. The runner prints only the browser basename, URL and sign-in visibility checks. It does not print page content or credentials.

This is a real browser render smoke check, not the complete Admin/Manager/Employee acceptance suite. Full journey automation still requires a dedicated isolated synthetic API/worker environment and a credential-approved live Gemini check. Do not point write workflows at a user's existing database.
