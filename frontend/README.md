# Frontend Layout

`frontend` contains UI applications separated by permission boundary.

## Apps

- `frontend/dashboard`: public read-only dashboard. It reads Supabase data only and must not access local copytrade credentials, account TOML files, or admin APIs.
- `frontend/admin`: local admin frontend. It is served by the copytrade admin backend for one-command local use, and can also run through Vite with `/api` proxied to the local admin backend.

## Compatibility

The root `dashboard` path is a temporary Windows junction to `frontend/dashboard`.

Old commands still work:

```powershell
cd dashboard
npm run build
```

Preferred commands:

```powershell
cd frontend\dashboard
npm run dev
npm run build
npm run preview
```

```powershell
cd frontend\admin
npm run dev
npm run build
```
