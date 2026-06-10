# Admin Frontend

Local-only management UI boundary for copytrade operations.

Current state:

- The admin UI lives in this folder as an independent local frontend app.
- The admin API remains in `backend/packages/copytrade/web/server.py`.
- The backend admin server also serves this `index.html`, so the one-command local flow still works.

Boundary rules:

- It may read and write account TOML files, masked credentials, maintenance actions, runtime status, alerts, and audit logs through the local admin API.
- It must not be exposed as the public dashboard.
- It must not sync local credentials or admin APIs to Supabase.

Future migration target:

- Split the current single-file HTML into maintainable components after the admin API contract is stable.

## Commands

One-command local admin:

```powershell
python backend\src\copytrade_admin.py
```

Open `http://127.0.0.1:8199`.

Frontend-only dev mode:

```powershell
cd frontend\admin
npm install
npm run dev
```

Frontend dev mode expects the backend admin API to be running on `http://127.0.0.1:8199` and proxies `/api` to it.
