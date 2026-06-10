# Backend Entry Layer

This directory is reserved for CLI, API, scheduler, and worker entrypoints.

Business logic must stay out of this layer. Entry files should parse input, load configuration, wire dependencies, and delegate into `backend/packages/copytrade`.

Current entrypoints:

- `copytrade_worker.py` delegates to `copytrade.main`.
- `copytrade_watchdog.py` delegates to `copytrade.watchdog`.
- `copytrade_admin.py` delegates to `copytrade.web.server`.
