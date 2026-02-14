# Nexus AI Portal

An enterprise AI platform combining natural-language business intelligence
(NL → SQL → dashboards/reports/PPT), general-purpose AI agents with tools and
workflows, a document knowledge base with RAG search, and a multi-model chat
workspace — all on top of a single SQL Server database.

See [`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md) for the architecture and
[`LOCAL_TESTING_GUIDE.md`](LOCAL_TESTING_GUIDE.md) for how to run it locally.

## Getting started

```powershell
python -m venv venv
venv\Scripts\pip install -r requirements.txt --no-deps
venv\Scripts\pip install waitress
```

Copy `.env` with your own database and API key configuration (see
`config.py` for the full settings surface), then run:

```powershell
venv\Scripts\python.exe app.py
```

## Deployment

For running as a Windows service, see
[`docs/NSSM_DEPLOYMENT.md`](docs/NSSM_DEPLOYMENT.md).
