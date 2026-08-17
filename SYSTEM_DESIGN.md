# System Design — Quick Read Before the Interview

## 1. The pitch (say this first)

One internal Flask web app. Three AI features, one shared SQL Server
database, one OpenAI account. No message queue, no separate vector DB
server, no microservices — deliberately boring infra, added complexity
only where it was actually needed.

The three features:
1. **BI / NLQ** — ask a question in English → AI writes SQL → chart/report/PPT.
2. **Hub agents** — general AI assistant with tools, workflows, human approvals.
3. **Scheduled jobs** — the above two, run on a timer, emailed automatically.

## 2. Architecture diagram

```
                 Browser
                    │
              Flask app (one process, blueprints = feature modules)
        ┌───────────┼───────────────┐
        ▼           ▼               ▼
   BI / NLQ    Hub agents /    Scheduled jobs
   (SQL gen)   RAG / tools     (timer + email)
        └───────────┼───────────────┘
                    ▼
         ┌─────────────────────┐
         │     SQL Server       │  ← single source of truth:
         │ users, auth, jobs,   │    auth, permissions, chat
         │ chat history, logs   │    history, job logs, metadata
         └─────────────────────┘
                    │
         ┌─────────────────────┐
         │      LanceDB          │  ← local vector index for
         │ (document search)     │    document/RAG search
         └─────────────────────┘
                    │
               OpenAI API   ← only LLM provider
```

## 3. The three flows (30 seconds each)

**BI/NLQ** — "top 5 clients last quarter?"
Question → a small embedding model picks the ~20 relevant tables (not the
whole schema) → a pre-built join graph tells it how they connect → AI
writes SQL → runs against the business DB → rendered as chart/report/PPT.
*Why: narrowing the schema first keeps it fast and accurate as the DB grows.*

**RAG / documents** — "what's our return policy?"
Doc uploaded → split into overlapping chunks → embedded → stored in
LanceDB. On query: check cache → vector search (top 20) → re-score by
keyword match too (hybrid) → optional AI re-rank → confidence score per
result → if confidence too low, tell the AI "not enough evidence" instead
of guessing (hallucination guard). Access control is applied *before*
search, not filtered after.

**Hub agents** — general assistant with tools
Loop: ask AI what to do → if it wants a tool (search docs, send email,
query DB…), run it, feed result back → repeat until final answer or a
loop limit. Multi-step chains (workflows) can pause for a **human
approval** click before continuing — for anything risky.

**Scheduled jobs**
Job = row in DB (agent, question, outputs, schedule, delivery email).
Scheduler wakes it up, re-reads it fresh, re-runs the BI flow, logs every
step, emails the result. One output failing (e.g. PPT) doesn't block the
others (e.g. dashboard still sent).

## 4. The one big decision

Everything lives in **one SQL Server DB** — no separate vector DB, job
queue, or graph DB. Only exception: document vectors live in **LanceDB**
(a lightweight file-based index, not a server) because brute-force search
in SQL doesn't scale once there are many chunks.

**Say this**: "Each extra piece of infra (queue, vector DB, graph DB) was
evaluated and skipped unless it solved a real problem. That's a stronger
answer than 'we used everything.'"

## 5. How it stays fast

| Trick | What it means |
|---|---|
| Buffer, then batch-write | Chat streams to the screen instantly; DB writes happen after, on a background thread |
| Narrow before you ask AI | Only send the relevant ~20 tables/chunks, not everything |
| Cache repeat questions | Same question/user within a few minutes → served from cache |
| Index only once there's enough data | Brute-force search until data crosses a size threshold, then real ANN index kicks in |
| One writer at a time | All vector-index writes go through a single background thread — avoids concurrency bugs |
| Don't stack duplicate runs | If a job's still running at its next scheduled time, skip the duplicate |
| Soft-fail optional stuff | OCR, re-ranking, file watchers — missing dependency just disables that feature, doesn't crash the app |

## 6. Security / multi-tenancy

- Roles: admin / dev / user. Bcrypt passwords, server-side sessions (no JWT).
- Users belong to departments/projects → data scoped to those.
- BI agents have **guardrails**: rules merged into the AI's prompt so row
  access is restricted *before* SQL runs — not filtered after the fact.
- Same idea for document search: visibility filter applied at query time.

## 7. Honest trade-offs (say these if asked "what would you improve")

- Two independent job schedulers (BI jobs vs Hub tool jobs) — never merged, just tidiness debt.
- Two LLM integration styles: BI uses LangChain, Hub agents use raw HTTP to OpenAI — built at different times.
- Same embedding model loaded twice in memory (RAG + schema-matching) — not shared.
- Single SQL Server today = single point of failure — fine at current scale, but no failover yet.

## 8. Scaling path (cloud migration, no rewrite needed)

```
SQL Server (local)     → Azure SQL             (connection string change)
Local file storage     → Azure Blob Storage    (one storage adapter)
LanceDB (local)        → Azure AI Search       (already coded, env-var switch)
OpenAI direct          → Azure OpenAI          (endpoint + deployment name)
```

**Say this**: "The seams are already there. Scaling up means swapping one
piece at a time via config, not rewriting the app."

## 9. If asked "why not microservices/Kafka from day one?"

"Because this system doesn't have the problem those solve yet. One Flask
app + one DB handles internal company traffic fine. Adding a queue or
splitting services early adds deployment complexity without adding
capability. Instead, the boundaries are already clean — a vector-store
interface with three swappable backends, one DB connection string — so
when something *does* become a bottleneck, you change that one piece, not
the whole system."
