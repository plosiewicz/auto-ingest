# auto-ingest

A self-contained CLI for the AutoGraph ingestion pipeline. Two modes:

1. **Use an existing AutoGraph deployment** (you already have a URL).
2. **Auto-deploy a fresh AutoGraph deployment** (just provide an OpenAI key).

It picks between the two based on what you've put in `.env` — no flags
required for the common case.

Optionally converts source documents (PDF / DOCX / PPTX / TXT) to Markdown
first.

## What it does

| Phase | Endpoint | What happens |
|---|---|---|
| **A. import** | `POST /v1/import-multiple` | Uploads base64-encoded markdown to AutoGraph. Auto-batches at 50 MB. |
| **B. corpus build** | `POST /v1/corpus/builds` + `GET .../{id}` | Chunks, embeds, builds similarity edges, clusters. Polls until terminal status. |
| **C. strategizer** | `POST /v1/rag-strategizer/analyze` + `GET /strategy` | Assigns RAG strategies per partition. Polls until two consecutive responses match. |
| **D. orchestrate** | `POST /v1/orchestrate` | Materializes the actual graph in the background. Returns immediately; verify completion via the AI Suite UI. |

Plus, optionally:

| Step | What happens |
|---|---|
| **provision** | ACP `POST /database` → `POST /project` → `POST /autograph` → wait for `DEPLOYED` → probe public URL → write `provisioned_service.json`. |
| **teardown** | Reverse of provision: `DELETE /service/<id>` → `DELETE /project/...` → `DELETE /database/...` (optional). |

## Prerequisites

- Python 3.10+
- An ArangoDB cluster URL + username + password (the same cluster the AutoGraph
  service is — or will be — deployed against).
- One of:
  - An **existing** AutoGraph public URL (looks like `https://<host>.rnd.pilot.arango.ai/autograph/<5-char-suffix>`), OR
  - An **OpenAI API key** (only required if you're auto-deploying a new AutoGraph; it gets stored server-side at deploy time, the script never re-reads it).

## Setup

```bash
git clone <this-repo-url> auto-ingest
cd auto-ingest

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in ARANGO_PASSWORD plus EITHER AUTOGRAPH_API_URL or OPENAI_API_KEY
```

To preview which mode you're in before running anything heavy:

```bash
python ingest.py status
```

## Usage

### Mode A — point at an existing AutoGraph

Set `AUTOGRAPH_API_URL` in `.env`. Then:

```bash
python ingest.py ingest --md-dir ./md-output -v
```

No OpenAI key required. The four phases run end-to-end.

### Mode B — let auto-ingest deploy AutoGraph for you

Leave `AUTOGRAPH_API_URL` blank in `.env`, set `OPENAI_API_KEY`. Then either:

**Option 1 — one-shot (auto-provision happens inside ingest):**
```bash
python ingest.py ingest --md-dir ./md-output -v
# detects no URL, no cache → provisions automatically, then ingests.
# Cache is written to ./provisioned_service.json so subsequent runs skip provisioning.
```

**Option 2 — explicit provision then ingest:**
```bash
python ingest.py provision -v
# Provisions DB + project + AutoGraph service. Writes provisioned_service.json.

python ingest.py ingest --md-dir ./md-output -v
# Reads provisioned_service.json → ingests against the new service.
```

### Convert source docs and ingest in one shot

```bash
python ingest.py ingest \
  --source-dir ./source-docs \
  --md-dir ./md-output \
  -v
```

The `--md-dir` directory will be created. A `markdown_conversion_log.csv`
is written next to it for auditing which converter ran on each file.

### Just convert (no ingestion)

```bash
python markdown_convert.py -i ./source-docs -o ./md-output -v
```

### Teardown (Mode B only — undoes a provision)

```bash
python ingest.py teardown          # dry-run
python ingest.py teardown --yes    # actually deletes service+project+db
python ingest.py teardown --yes --keep-db  # keep the DB for inspection
```

## Auto-detection priority

Inside `python ingest.py ingest`, the AutoGraph URL is resolved in this order:

1. `AUTOGRAPH_API_URL` env var — use it (Mode A).
2. `./provisioned_service.json` — use it (cached from a previous `provision`).
3. `OPENAI_API_KEY` set + (defaults or `ARANGO_INGEST_DB_NAME` set) — auto-provision then ingest (Mode B).
4. Otherwise — fail with a helpful error pointing at one of the above.

To disable step 3 (refuse to auto-provision), pass `--no-auto-provision`.

## Useful flags (subset)

| Flag | Default | Purpose |
|---|---|---|
| `--module` | `auto_ingest` (or `$AUTOGRAPH_MODULE_LABEL` / cached value) | Tag uploaded docs with this label. |
| `--top-k` | `7` | Corpus-build similarity-edge top-k. |
| `--cluster-threshold` | `2` | Corpus-build cluster threshold. |
| `--replicas` | `1` | Phase D worker replicas. |
| `--poll-interval-s` | `10.0` | Polling interval for Phase B / C. |
| `--corpus-timeout-s` | `14400` (4 h) | Phase B max wall-time. |
| `--strategy-timeout-s` | `1800` (30 min) | Phase C max wall-time. |
| `--orchestrate-prior-wait-s` | `1800` (30 min) | Max wait for any prior orchestration to clear. |
| `--skip-import` / `--skip-corpus-build` / `--skip-strategizer` / `--skip-orchestrate` | — | Re-run individual phases. |
| `--force-convert` | `false` | Reconvert source docs even if a fresh `.md` exists. |
| `--no-auto-provision` | `false` | Refuse to auto-deploy; require URL or cache. |

For the full list, run `python ingest.py ingest --help` (or `provision --help`,
`teardown --help`, etc.).

## Layout

```
auto-ingest/
  ingest.py              # the driver (CLI dispatcher: ingest / provision / teardown / status)
  provision.py           # deploy + teardown + URL resolution + state persistence
  autograph_client.py    # AutoGraph HTTP client (JWT auth, retries, batching)
  acp_client.py          # ArangoDB Control Plane client (database/project/service CRUD)
  markdown_convert.py    # PDF/DOCX/PPTX/TXT -> .md (markitdown + pandoc fallback)
  requirements.txt
  .env.example
  README.md
```

`provisioned_service.json` is written next to the script after `provision`
runs successfully (gitignored — contains a service ID and URL but no secrets).

## What the output looks like

```
$ python ingest.py ingest --md-dir ./md-output -v
2026-05-11 12:30:01 INFO auto_ingest: Found 11 markdown file(s) in ./md-output
2026-05-11 12:30:01 INFO auto_ingest: AutoGraph URL: https://zgubculb.rnd.pilot.arango.ai/autograph/5iimg   (source: cache)
2026-05-11 12:30:01 INFO auto_ingest: Module label : auto_ingest
2026-05-11 12:30:02 INFO auto_ingest: Health: {'status': 'healthy'}
2026-05-11 12:30:02 INFO auto_ingest: === Phase A: import-multiple (11 files) ===
2026-05-11 12:30:08 INFO auto_ingest: Phase A done in 5.4s: 11 files, 591234 bytes, 1 batch(es)
2026-05-11 12:30:08 INFO auto_ingest: === Phase B: corpus build (module=auto_ingest, top_k=7, cluster_threshold=2) ===
2026-05-11 12:30:09 INFO auto_ingest: Phase B kicked off in 312ms (build_id=abc123) — polling every 10s ...
2026-05-11 12:30:19 INFO auto_ingest: Phase B poll #1 status=in_progress progress=0.18 msg=chunking
... (5–30 min depending on corpus size) ...
2026-05-11 12:42:11 INFO auto_ingest: Phase B done in 723.4s (72 polls)
2026-05-11 12:42:11 INFO auto_ingest: === Phase C: rag-strategizer ===
2026-05-11 12:42:42 INFO auto_ingest: Phase C done in 31.2s (3 polls, 4 strategies, stabilized after 30810ms)
2026-05-11 12:42:42 INFO auto_ingest: === Phase D: orchestrate (replicas=1) ===
2026-05-11 12:42:43 INFO auto_ingest: Phase D kicked off in 187ms (waited 0.0s for prior orchestration to clear)
2026-05-11 12:42:43 INFO auto_ingest:   total_jobs=4 completed=0 failed=0
2026-05-11 12:42:43 INFO auto_ingest: Phase D kickoff done in 0.2s.
2026-05-11 12:42:43 INFO auto_ingest: NOTE: orchestrate kickoff returns immediately. ...
Ingestion finished in 762.0s.
```

## Caveats

- **Phase D doesn't wait for graph completion.** `POST /v1/orchestrate`
  returns as soon as the orchestration job is scheduled. The graph keeps
  materializing in the background for another 3–15 minutes (corpus-size
  dependent). Verify completion in the AI Suite UI by inspecting collection
  counts on the target DB, or query ArangoDB directly. The benchmarking
  variant in the upstream `wtw-benchmark` repo (`src/ingest_benchmark.py`)
  implements a collection-count watch heuristic; we deliberately don't pull
  that in here because it requires direct ArangoDB DB access (`python-arango`
  dep).
- **Only one orchestration at a time per AutoGraph service.** If a previous
  ingestion is still running, Phase D will return 409 and `ingest.py` will
  wait up to 30 min for it to clear.
- **`incremental=False`.** Phase B is run with `incremental=false`, meaning
  the corpus is rebuilt from scratch each time. If you want incremental
  ingestion (add docs to an existing corpus), edit `phase_b_corpus_build`
  in `ingest.py` to pass `incremental=True` to `create_corpus_build`.
- **AutoGraph URL auto-discovery can take ~90s.** AI Suite route propagation
  on a fresh deploy isn't instant. The `provision` command probes 4 URL
  patterns for up to 180 seconds. If you already know the URL from the AI
  Suite UI, pass `python ingest.py provision --api-url https://.../autograph/<suffix>`
  to skip discovery.
- **TLS verification off by default.** Most PoC clusters don't have a
  publicly trusted cert. Override with `ARANGO_TLS_VERIFY=true` in `.env`
  if your cluster does.
- **`provisioned_service.json` is git-ignored** but does contain the service
  ID and DB name — treat it as session state rather than committed config.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Auth request failed: ... 401` | `ARANGO_PASSWORD` is wrong, or `ARANGO_USER` doesn't exist on the cluster. |
| `404 Not Found` on `/v1/health` | `AUTOGRAPH_API_URL` is wrong, or auto-discovery picked the wrong candidate. Re-run `provision --api-url ...` with the URL from the UI. |
| `409 OrchestrationInProgressError` | Another ingestion is already running on this AutoGraph service. The script will retry for up to 30 min by default. |
| `Could not resolve AutoGraph URL within 180s` | AI Suite routing is taking longer than usual. Retry, or grab the URL from the UI and pass `--api-url`. |
| Phase B polls forever with `progress=null` | The corpus build is genuinely slow on big corpora; let it run, or raise `--corpus-timeout-s`. |
| Phase C never stabilizes | The strategizer keeps producing different responses. Usually means the corpus is too small. Try `--cluster-threshold 1` or upload more docs. |
| `Missing required env var: OPENAI_API_KEY` | You're in Mode B (no URL set, no cache) but didn't supply an OpenAI key for auto-provision. Either set the key, or set `AUTOGRAPH_API_URL` to point at an existing service. |

## Where this came from

This is a stripped-down extract from `wtw-benchmark` — specifically
`src/autograph_client.py`, `src/acp_client.py`, `src/markdown_convert.py`,
and the per-phase orchestration in `src/ingest_benchmark.py`. The full
benchmark adds: multi-scale sweeps, streaming event recording, graph
snapshots, safety diff against production, and a markdown report. Use that
if you need timings or a full audit trail; use this if you just want to
push docs into a graph (and optionally deploy AutoGraph itself).
