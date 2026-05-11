# auto-ingest

A self-contained, copy-paste-able script for pushing Markdown documents into
an existing AutoGraph deployment via the AutoGraph HTTP API.

Optionally converts source documents (PDF / DOCX / PPTX / TXT) to Markdown
first.

This repo is intentionally standalone — clone it, install the requirements,
fill in `.env`, and you're running.

## What it does

Runs the four AutoGraph ingestion phases against a deployment you already
have:

| Phase | Endpoint | What happens |
|---|---|---|
| **A. import** | `POST /v1/import-multiple` | Uploads base64-encoded markdown to AutoGraph. Auto-batches at 50 MB. |
| **B. corpus build** | `POST /v1/corpus/builds` + `GET .../{id}` | Chunks, embeds, builds similarity edges, clusters. Polls until terminal status. |
| **C. strategizer** | `POST /v1/rag-strategizer/analyze` + `GET /strategy` | Assigns RAG strategies per partition. Polls until two consecutive responses match. |
| **D. orchestrate** | `POST /v1/orchestrate` | Materializes the actual graph in the background. Returns immediately; verify completion via the AI Suite UI. |

It does **not** provision new AutoGraph services and does **not** include
any benchmarking instrumentation, snapshots, or safety-diff. If you need
those, see the upstream `wtw-benchmark` repo (`src/ingest_benchmark.py`),
which this is extracted from.

## Prerequisites

You need an **already-deployed AutoGraph service** plus credentials:

- An AutoGraph public URL (looks like `https://<host>.rnd.pilot.arango.ai/autograph/<5-char-suffix>`).
  Find it in the AI Suite UI under *GenAI Services → AutoGraph → your service*.
- ArangoDB cluster URL + username + password (the same cluster the AutoGraph
  service is deployed against).
- Python 3.10+.

You do **not** need an OpenAI key on this side — AutoGraph uses its own
configured embedding model server-side.

## Setup

```bash
git clone <this-repo-url> auto-ingest
cd auto-ingest

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in ARANGO_PASSWORD and AUTOGRAPH_API_URL
```

## Usage

### Already have markdown files

```bash
python ingest.py --md-dir /path/to/markdown/ -v
```

### Convert source docs (PDF/DOCX/PPTX) and ingest in one shot

```bash
python ingest.py \
  --source-dir /path/to/source-docs/ \
  --md-dir /path/to/converted-md/ \
  -v
```

The `--md-dir` directory will be created if it doesn't exist. A
`markdown_conversion_log.csv` is written next to it for auditing which
converter ran on each file.

### Just convert (no ingestion)

```bash
python markdown_convert.py -i /path/to/source-docs/ -o /path/to/converted-md/ -v
```

### Useful flags

| Flag | Default | Purpose |
|---|---|---|
| `--module` | `wtw_ingest` (or `$AUTOGRAPH_MODULE_LABEL`) | Tag uploaded docs with this label. AutoGraph uses it to scope corpus builds. |
| `--top-k` | `7` | Corpus-build similarity-edge top-k. |
| `--cluster-threshold` | `2` | Corpus-build cluster threshold. |
| `--replicas` | `1` | Phase D worker replicas. |
| `--poll-interval-s` | `10.0` | How often to poll Phase B / C. |
| `--corpus-timeout-s` | `14400` (4 h) | Phase B max wall-time. |
| `--strategy-timeout-s` | `1800` (30 min) | Phase C max wall-time. |
| `--orchestrate-prior-wait-s` | `1800` (30 min) | Max wait for any prior orchestration to clear before kicking ours off. |
| `--skip-import` / `--skip-corpus-build` / `--skip-strategizer` / `--skip-orchestrate` | — | Re-run individual phases. |
| `--force-convert` | `false` | Reconvert source docs even if a fresh `.md` exists. |

## Layout

```
auto-ingest/
  ingest.py                # the driver script (this is what you run)
  autograph_client.py      # HTTP client — JWT auth, retries, batching
  markdown_convert.py      # PDF/DOCX/PPTX/TXT -> .md (markitdown + pandoc fallback)
  requirements.txt
  .env.example
  README.md
```

## What the output looks like

```
$ python ingest.py --md-dir ./md-output -v
2026-05-11 12:30:01 INFO auto_ingest: Found 11 markdown file(s) in ./md-output
2026-05-11 12:30:01 INFO auto_ingest: AutoGraph URL: https://zgubculb.rnd.pilot.arango.ai/autograph/5iimg
2026-05-11 12:30:01 INFO auto_ingest: Module label : wtw_ingest
2026-05-11 12:30:02 INFO auto_ingest: Health: {'status': 'healthy'}
2026-05-11 12:30:02 INFO auto_ingest: === Phase A: import-multiple (11 files) ===
2026-05-11 12:30:08 INFO auto_ingest: Phase A done in 5.4s: 11 files, 591234 bytes, 1 batch(es)
2026-05-11 12:30:08 INFO auto_ingest: === Phase B: corpus build (module=wtw_ingest, top_k=7, cluster_threshold=2) ===
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
  benchmarking variant in the upstream `wtw-benchmark` repo
  (`src/ingest_benchmark.py`) implements a collection-count watch heuristic;
  we deliberately don't pull that in here because it requires direct ArangoDB
  DB access (`python-arango` dep).
- **Only one orchestration at a time per AutoGraph service.** If a previous
  ingestion is still running, Phase D will return 409 and `ingest.py` will
  wait up to 30 min for it to clear (tunable via
  `--orchestrate-prior-wait-s`).
- **`incremental=False`.** Phase B is run with `incremental=false`, meaning
  the corpus is rebuilt from scratch each time. If you want incremental
  ingestion (add docs to an existing corpus), edit `phase_b_corpus_build`
  in `ingest.py` to pass `incremental=True` to `create_corpus_build`.
- **TLS verification off by default.** The PoC cluster doesn't have a
  publicly trusted cert. Override with `ARANGO_TLS_VERIFY=true` in `.env`
  if you're hitting a cluster that does.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Auth request failed: ... 401` | `ARANGO_PASSWORD` is wrong, or `ARANGO_USER` doesn't exist on the cluster. |
| `404 Not Found` on `/v1/health` | `AUTOGRAPH_API_URL` is wrong. Double-check the suffix in the AI Suite UI. |
| `409 OrchestrationInProgressError` | Another ingestion is already running on this AutoGraph service. The script will retry for up to 30 min by default. |
| Phase B polls forever with `progress=null` | The corpus build is genuinely slow on big corpora; let it run, or raise `--corpus-timeout-s`. |
| Phase C never stabilizes | The strategizer keeps producing different responses. Usually means the corpus is too small (< 2 docs per cluster). Try `--cluster-threshold 1` or upload more docs. |

## Where this came from

This is a stripped-down extract from `wtw-benchmark` — specifically
`src/autograph_client.py`, `src/markdown_convert.py`, and the per-phase
orchestration in `src/ingest_benchmark.py`. The full benchmark adds:
provisioning, multi-scale sweeps, streaming event recording, graph
snapshots, safety diff against production, and a markdown report. Use that
if you need timings or a full audit trail; use this if you just want to
push docs into a graph.
