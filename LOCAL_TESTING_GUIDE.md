# Local Testing Guide — Nexus AI Portal

How to stand this project up **fully locally**, from a clean checkout, without
ever touching the real production database, backup files, or production-billed
credentials. Follow top to bottom on a fresh setup; skip ahead if a step is
already done. See `PROJECT_OVERVIEW.md` for what the app actually *does* —
this file is only the "how do I get it running" procedure.

## 0. Safety ground rules — read this first

- **Never point `DB_SERVER` at the real production SQL Server address**
  for local testing. Always use a local SQL Server instance with your own
  fresh, empty database.
- **Never restore a production `.bak` backup** (they contain real production
  data, not source code) into anything the app connects to. If you need to
  inspect real schema/data for reference, restore it under an
  obviously-named separate database and never wire the app's `.env` to it.
- **Never reuse the committed/real `.env`** for local experiments. Copy it
  aside first (outside the repo, e.g. your own scratch folder) if you need
  to preserve it, then work from a fresh local-only `.env`.
- If a database matching the production name already exists on your local
  SQL Server instance, treat it as **read-only reference** — don't drop it,
  don't point the app at it. Create a differently-named database
  (`nexus_local_dev` below) for actual testing.

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.12 | `requirements.txt` pins modern package versions (numpy 2.x, pandas 3.x) that need 3.10+. Check installs with `py -0` or look in `%LOCALAPPDATA%\Programs\Python\`. |
| SQL Server (any local instance) | SQL Server Express is fine. Check with `sc query state= all` (look for `MSSQL$<instance>`) or install SQL Server Express + SSMS if you don't have one. |
| ODBC Driver 17 or 18 for SQL Server | Check via PowerShell: `Get-OdbcDriver \| Where-Object Name -like "*SQL Server*"`. Install from Microsoft if missing — the app's default is `ODBC Driver 17 for SQL Server`. |
| Admin rights on your machine | Needed once, to restart the SQL Server service after enabling TCP/IP and mixed-mode auth (steps 2–3). |

## 2. Configure your local SQL Server instance

Two things are usually **off by default** on a local/Express install and
both need to be enabled, or the app can't connect at all.

### 2.1 Enable TCP/IP (Express defaults to Shared Memory only)

pyodbc connects over TCP, so if TCP/IP is disabled you'll see errors like
`TCP Provider: No connection could be made because the target machine
actively refused it`.

1. Open **SQL Server Configuration Manager** (Start menu search, or run
   `SQLServerManager17.msc` — version number may differ).
2. **SQL Server Network Configuration → Protocols for `<your instance>`**.
3. Right-click **TCP/IP** → **Enable**.
4. Double-click **TCP/IP** → **IP Addresses** tab → scroll to **IPAll** at
   the bottom → set **TCP Port** to `1433`, clear **TCP Dynamic Ports**.

### 2.2 Enable SQL Server (mixed-mode) authentication

The app always connects via a SQL username/password — never Windows Auth —
so if your instance is Windows-Auth-only (the default for many local
installs), logins will fail even with TCP/IP working.

Run this once, from SSMS or `sqlcmd`/`Invoke-Sqlcmd`, connected to your
instance:

```sql
EXEC xp_instance_regwrite N'HKEY_LOCAL_MACHINE',
  N'Software\Microsoft\MSSQLServer\MSSQLServer', N'LoginMode', REG_DWORD, 2;
```

Check whether it's already mixed-mode first:
```sql
SELECT SERVERPROPERTY('IsIntegratedSecurityOnly'); -- 1 = Windows-only, 0 = mixed
```

### 2.3 Restart the SQL Server service (admin required)

Both changes above only take effect after a restart. From an **elevated**
PowerShell or Command Prompt:

```powershell
Restart-Service -Name 'MSSQL$<YOUR_INSTANCE>' -Force
```
(or via `services.msc` → right-click **SQL Server (\<instance\>)** → Restart)

Verify:
```powershell
Test-NetConnection -ComputerName localhost -Port 1433   # TcpTestSucceeded: True
```

## 3. Create a local database + dedicated login

Don't reuse any existing database. Create a fresh one and a login scoped
only to it:

```sql
CREATE DATABASE nexus_local_dev;
GO
CREATE LOGIN nexus_local WITH PASSWORD = '<pick-a-strong-local-only-password>', CHECK_POLICY = ON;
GO
USE nexus_local_dev;
CREATE USER nexus_local FOR LOGIN nexus_local;
ALTER ROLE db_owner ADD MEMBER nexus_local;
```

## 4. Build the schema

Most of the app's tables are created automatically at startup by
`database/app_db.py`'s `ensure_tables()` — but the auth, Hub, workspace, and
training tables live in hand-written scripts under `sql/` and must be run
**once, in this order** (later ones depend on earlier ones):

1. `sql/auth_setup.sql`
2. `sql/agents_hub_tables.sql`
3. `sql/hub_tables.sql`
4. `sql/hub_approvals.sql`
5. `sql/hub_custom_tools.sql`
6. `sql/workspace_tables.sql`
7. `sql/workspace_migrations_001.sql`
8. `sql/training_tables.sql` (needs `workspace_tables.sql` first)
9. `sql/bi_training_tables.sql`

Then the incremental `migrations/*.sql` files (all idempotent,
`IF NOT EXISTS`-guarded):
- `migrations/add_env_venv_columns.sql`
- `migrations/add_project_notes.sql`
- `migrations/add_token_io_and_org_columns.sql`
- `migrations/add_tool_jobs_columns.sql`
- Skip `migrations/add_model_to_token_usage.sql` — its own header says it's
  reference-only now and runs automatically via `ensure_tables()`.

**Two gotchas to know about before you run these:**

- `sql/auth_setup.sql`, `sql/agents_hub_tables.sql`, and
  `migrations/add_token_io_and_org_columns.sql` each hardcode
  `USE [nexus];` at the top. **Don't edit the files in place** —
  make a temp copy with that string replaced with your local DB name
  (`nexus_local_dev`) and run the copy instead, e.g.:
  ```powershell
  (Get-Content sql\auth_setup.sql) -replace '\[nexus\]', '[nexus_local_dev]' |
    Set-Content C:\temp\auth_setup.local.sql
  ```
- `sql/workspace_migrations_001.sql` has no `GO` batch separators between
  its three numbered steps. Running it as one batch fails with
  `Invalid column name 'target_user_id'` — SQL Server compiles the whole
  batch up front, so a `CREATE INDEX` referencing a column added earlier in
  the *same* batch by a conditional `ALTER TABLE` won't resolve. Fix: split
  it into 3 batches with `GO` between each numbered section (again, in a
  temp copy, not the repo file) before running.

Run each script against `nexus_local_dev`, e.g. from PowerShell:
```powershell
Invoke-Sqlcmd -ServerInstance "localhost\<instance>" -Database "nexus_local_dev" -InputFile "<path-to-script>"
```

`auth_setup.sql` seeds a default admin user (`admin@yourdomain.com`,
role `admin`). **Its shipped password hash does not actually match the
documented password `Admin@123`** — verify before relying on it:
```python
import bcrypt
bcrypt.checkpw(b"Admin@123", b"<hash-from-the-users-table>")  # returns False
```
If it's `False` for you too, generate a real hash and update the row:
```python
import bcrypt
print(bcrypt.hashpw(b"Admin@123", bcrypt.gensalt(12)).decode())
```
```sql
UPDATE users SET password_hash = '<new-hash>' WHERE username = 'admin';
```

## 5. Python environment

```powershell
# from the project root, using a Python 3.12 interpreter
python -m venv venv
venv\Scripts\pip install -r requirements.txt --no-deps
venv\Scripts\pip install waitress
```
`--no-deps` matches `start_server.bat` — `requirements.txt` is already a
fully flattened, pinned dependency list, so dependency resolution is
unnecessary and slower.

Sanity check the install:
```powershell
venv\Scripts\python -c "import flask, pyodbc, torch, transformers, sentence_transformers, langchain, openai; print('OK')"
```

## 6. Local `.env`

Create a **new** `.env` at the project root (don't overwrite a real one
without backing it up first — copy it somewhere outside the repo). Minimum
to get the app running:

```env
# OPENAI_API_KEY=<parked — see llm_providers/factory.py>
DEFAULT_LLM_PROVIDER=gemini
GEMINI_API_KEY=<your own personal key — https://aistudio.google.com/apikey>
OPENROUTER_API_KEY=<your own personal key, optional — used as a fallback provider>

DB_SERVER=localhost\<your instance>
DB_PORT=1433
DB_DATABASE=nexus_local_dev
DB_USERNAME=nexus_local
DB_PASSWORD=<the password you set in step 3>
DB_DRIVER=ODBC Driver 17 for SQL Server

SECRET_KEY=<random 64-char hex — generate with: python -c "import secrets; print(secrets.token_hex(32))">
FLASK_ENV=development
APPROVAL=false
```

Everything else (`SMTP_*`, `TWILIO_*`, `SP_*`, `SENTRY_DSN`,
`TEAMS_WEBHOOK_URL`, `TENANT_ID`/`BOT_APP_*`) can stay blank — each feature
just logs a warning and no-ops if unset; the app still starts. Fill in
`SMTP_*` if you want to test the login flow end-to-end (see §8).

### 6.1 Amazon Bedrock (parked — see `llm_providers/`)

The app talks to LLMs through a provider-agnostic abstraction
(`llm_providers/`) that currently ships Gemini and OpenRouter (both free-tier,
set up above) as the active providers. Bedrock is parked until AWS access is
sorted out — the code path still exists, just not wired as a default. Leave
these blank unless you're specifically re-enabling Bedrock:

```env
DEFAULT_LLM_PROVIDER=bedrock        # set back to "gemini" (or "openrouter") to undo this
AWS_REGION=<e.g. us-east-1 — required for any Bedrock use, even per-agent>

# Option A — Bedrock API key (bearer token). Some AWS accounts issue these
# from the Bedrock console's own "API keys" page (Long-term keys — NOT
# short-term, which expire in 12h and won't survive a server restart).
# Picked up automatically by boto3/botocore (>=1.40ish; pinned to 1.43.67
# here) via this exact env var name — no code involved, botocore derives it
# from the service's signing name ("bedrock" -> AWS_BEARER_TOKEN_BEDROCK).
# Takes priority over Option B below for Bedrock calls specifically.
AWS_BEARER_TOKEN_BEDROCK=<paste a Long-term key from Bedrock console -> API keys>

# Option B — standard IAM access key. Optional — omit both to fall back to
# boto3's default credential chain (IAM role, AWS CLI profile, etc) instead.
AWS_ACCESS_KEY_ID=<optional>
AWS_SECRET_ACCESS_KEY=<optional, pairs with AWS_ACCESS_KEY_ID>
AWS_SESSION_TOKEN=<optional — only needed for temporary/STS credentials>
```

Why both exist: some newer Bedrock console surfaces gate `Converse`/`InvokeModel`
behind this bearer-token mechanism even when a normal IAM user has
`AmazonBedrockFullAccess` attached (symptom: `ListFoundationModels` succeeds,
but the actual invoke call returns `ValidationException: Operation not
allowed`). If you hit that, generate a Long-term Bedrock API key and set
`AWS_BEARER_TOKEN_BEDROCK` instead of relying on the IAM key alone.

Every feature's default model id **automatically follows
`DEFAULT_LLM_PROVIDER`** (see `llm_providers.models.resolve_default_model`)
— flipping it to `bedrock` does not require also hunting down and setting
a dozen `*_MODEL` env vars. Each call site declares a cost/quality tier
("fast" or "quality") and gets the matching model for whichever provider is
active: Gemini 2.5 Flash-Lite/Flash for Gemini, a free Llama/DeepSeek model
for OpenRouter, Claude Haiku/Sonnet for Bedrock. The `*_MODEL` env vars below
still exist as **explicit overrides**
for when you want one specific feature on a different model than the tier
default, not as something you must set on every provider switch:
`DASHBOARD_MODEL`, `REPORT_MODEL`, `INFOGRAPHIC_MODEL`, `OPENAI_PPT_MODEL`,
`OPENAI_PPT_VALIDATE_MODEL`, `OPENAI_MODEL` (hub document generation),
`DOCUMENT_METRICS_MODEL`, `DOCUMENT_SUMMARY_MODEL`, `WORKSPACE_TITLE_MODEL`,
`BEDROCK_NLQ_MODEL` (NL→SQL engine, both providers despite the name).

Hub Agents and Workspace chat don't need any of the model-override env
vars above — provider + model are picked per agent / per conversation in
the UI (a single model dropdown; the provider is inferred from the
selected model, tagged via each `<option>`'s `data-provider`).

Whichever AWS principal you use needs `bedrock:InvokeModel` and
`bedrock:InvokeModelWithResponseStream` for the model IDs you intend to
call, and those models must be enabled for your account in the AWS Bedrock
console (Model access) for the configured region — `boto3`'s default
credential chain and IAM permissions are entirely your AWS account's
responsibility, not something local testing here can substitute for.

## 7. Start the app

```powershell
venv\Scripts\waitress-serve.exe --host=127.0.0.1 --port=5008 app:app
```
(equivalent to what `start_server.bat` does after its own venv/pip-install bootstrapping).

Watch the console/log for these lines, in order — this is the healthy
startup sequence:
```
Logging initialised ...
document_watcher: watchdog Observer started
sharepoint_watcher: started (0 watch configs)
SENTRY_DSN not configured — Sentry disabled          [expected if blank]
LLM initialized
SchemaGraph disabled: set NEO4J_URI, ...              [expected — uses NetworkX instead]
SchemaGraph: using NetworkX + SQL Server backend
Warming up embedding model...                         [downloads all-MiniLM-L6-v2 on first run]
Warming up LLM...
Warmup complete
PresentationGenerator ready
Serving on http://127.0.0.1:5008
```

If you see `app_db: portal sync disabled (set PORTAL_DB_NAME to enable)`
— that's expected; portal sync only activates if you configure
`PORTAL_DB_NAME`, which local testing doesn't do.

## 8. Verify it's actually working

```powershell
curl.exe -s -o NUL -w "STATUS:%{http_code}`n" http://127.0.0.1:5008/          # 302 → redirects to /login
curl.exe -s -o NUL -w "STATUS:%{http_code}`n" http://127.0.0.1:5008/login     # 200
```

Test DB-backed auth directly:
```powershell
curl.exe -s -X POST http://127.0.0.1:5008/api/auth/login `
  -H "Content-Type: application/json" `
  -d '{"email":"admin@yourdomain.com","password":"Admin@123"}'
```
- `{"status":"otp_required",...}` → password check against the DB passed.
- `{"status":"error","message":"Could not send OTP email..."}` → password
  check passed, only SMTP isn't configured (fine if you skipped §6's SMTP
  block).
- `{"status":"error","message":"Invalid email or password"}` → check the
  password hash issue in §4.

### To fully log in via the browser (OTP email)

The app needs real SMTP creds to deliver the one-time code. Easiest path —
a personal Gmail with an **App Password** (regular Gmail passwords are
rejected over SMTP):

1. Enable 2-Step Verification: `myaccount.google.com/security`
2. Create an app password: `myaccount.google.com/apppasswords`
3. Set in `.env`:
   ```env
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USER=<your gmail address>
   SMTP_PASS=<16-char app password, no spaces>
   SMTP_FROM=<your gmail address>
   SMTP_USE_TLS=true
   ```
4. Update the seeded admin user's email to that same inbox (the seeded
   value `admin@yourdomain.com` isn't a real, deliverable address):
   ```sql
   UPDATE users SET email = '<your gmail address>' WHERE username = 'admin';
   ```
5. Restart the app (env vars only load once, at process start —
   `load_dotenv()` in `app.py` won't pick up changes without a restart).
6. Open `http://127.0.0.1:5008/login`, sign in with `Admin@123`, grab the
   OTP from your inbox, done.

## 9. Restarting after a `.env` or DB change

The app reads `.env` once at startup. Any time you change `.env`, kill the
running process and start it again:
```powershell
# find it
Get-Process waitress-serve -ErrorAction SilentlyContinue
# or just Ctrl+C in the terminal it's running in, then re-run the command from §7
```

## 10. Troubleshooting quick reference

| Symptom | Cause | Fix |
|---|---|---|
| `TCP Provider: ... actively refused it` | TCP/IP disabled on the SQL instance | §2.1 |
| `Login failed for user` / connection refused with correct password | Instance is Windows-Auth-only | §2.2–2.3 |
| `Invalid column name 'target_user_id'` running `workspace_migrations_001.sql` | Script has no `GO` between batches | §4 gotcha |
| `Invalid email or password` on a fresh seed | Shipped admin hash doesn't match `Admin@123` | §4 hash fix |
| `Could not send OTP email` | `SMTP_USER`/`SMTP_PASS` blank or wrong | §8 SMTP setup |
| OTP request succeeds but no email ever arrives | Admin's seeded email (`admin@yourdomain.com`) isn't real | §8 step 4 |
| `app_db: portal sync disabled (set PORTAL_DB_NAME to enable)` | Expected — `PORTAL_DB_NAME` unset, feature no-ops | Ignore |
| `SchemaGraph disabled: set NEO4J_URI...` | Expected — falls back to NetworkX automatically | Ignore |

## 11. What to poke at once it's running

See `PROJECT_OVERVIEW.md` §3 for the two agent systems. Quick starting points:
- **BI/NLQ chat**: needs a BI agent wired to *some* database connection
  (`app_database_connections` table) — without one there's nothing for the
  NLQ engine to query yet.
- **Hub agents**: `hub_tools` gets auto-seeded with built-in tools on first
  run; create a Hub agent through the UI and chat with it — exercises the
  raw-HTTP OpenAI call path in `agents/core/orchestrator/engine.py`.
- **Document upload / RAG**: upload a PDF/docx through the Knowledge UI —
  exercises `document_processor.py` → chunking → `sentence-transformers`
  embedding → storage in `app_document_chunks`.
- **Workspace chat**: OpenAI-only multi-model chat UI, independent of the
  BI/Hub systems.
