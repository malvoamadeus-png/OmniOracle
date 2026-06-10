# Public Dashboard

Read-only public dashboard app.

## Boundary

- Reads Supabase views and tables needed for leader attribution, daily PnL comparison, and summarized runtime status.
- Does not read local account configuration, local credentials, or admin APIs.
- Remains deployable as an independent Vercel project.

## Commands

```powershell
cd frontend\dashboard
npm install
npm run dev
npm run build
npm run preview
```

The root `dashboard` path is currently a Windows junction to this folder, so old commands using `cd dashboard` remain valid during the migration period.

