"""End-to-end AutoGraph ingestion CLI.

Subcommands:

    ingest      Push a directory of Markdown through Phases A-D. Auto-detects
                whether to use an existing AutoGraph (env var or cached
                provisioned_service.json) or auto-provision one (if
                OPENAI_API_KEY + ARANGO_INGEST_DB_NAME are set).

    provision   Explicitly create the DB, project, and AutoGraph service.
                Writes provisioned_service.json next to the script for the
                ingest subcommand to pick up automatically.

    teardown    Reverse a `provision`: delete the service, project, and
                (optionally) the database.

    status      Show what's currently configured/provisioned.

The four AutoGraph ingestion phases run by `ingest`:

    Phase A: POST /v1/import-multiple                 (upload)
    Phase B: POST /v1/corpus/builds + GET poll        (chunk, embed, cluster)
    Phase C: POST /v1/rag-strategizer/analyze + poll  (assign RAG strategies)
    Phase D: POST /v1/orchestrate                     (materialize the graph)

Examples:

    # already have an AutoGraph URL (set in .env)
    python ingest.py ingest --md-dir ./md-output -v

    # convert source docs first, then ingest
    python ingest.py ingest --source-dir ./source-docs --md-dir ./md-output -v

    # explicit provision then ingest (re-runs reuse provisioned_service.json)
    python ingest.py provision -v
    python ingest.py ingest --md-dir ./md-output -v

    # cleanup
    python ingest.py teardown --yes -v
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from autograph_client import AutographClient, AutographError, FileSpec
from markdown_convert import convert_directory, write_log
from provision import (
    DEFAULT_DB_NAME,
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_CHAT_MODEL,
    DEFAULT_MODULE_LABEL,
    DEFAULT_PROJECT_NAME,
    DEFAULT_PROVISIONED_FILE,
    provision as do_provision,
    read_provisioned,
    teardown as do_teardown,
)

log = logging.getLogger("auto_ingest")


# ---------------------------------------------------------------------------
# env helpers
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _required_env(name: str) -> str:
    value = _env(name)
    if not value:
        raise SystemExit(
            f"Missing required env var: {name}. Copy .env.example to .env and fill it in."
        )
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _arango_creds() -> tuple[str, str, str, bool]:
    """(arango_url, user, password, tls_verify) — required for any subcommand."""
    return (
        _required_env("ARANGO_URL").rstrip("/"),
        _required_env("ARANGO_USER"),
        _required_env("ARANGO_PASSWORD"),
        _bool_env("ARANGO_TLS_VERIFY", default=False),
    )


# ---------------------------------------------------------------------------
# auto-detect: where does the AutoGraph URL come from?
# ---------------------------------------------------------------------------


def resolve_autograph_target(
    *,
    persist_path: Path,
    auto_provision: bool = True,
) -> dict:
    """Decide which AutoGraph deployment to ingest into.

    Priority:
      1. AUTOGRAPH_API_URL env var (treat as "already-deployed; just point at it").
      2. provisioned_service.json next to the script (cached from a previous
         provision run).
      3. Auto-provision if (a) auto_provision=True, (b) OPENAI_API_KEY is set,
         and (c) ARANGO_INGEST_DB_NAME is set.

    Returns a dict containing at least:
      - autograph_api_url
      - module_label
      - source ("env" | "cache" | "auto_provisioned")
    """
    explicit_url = _env("AUTOGRAPH_API_URL")
    if explicit_url:
        return {
            "autograph_api_url": explicit_url.rstrip("/"),
            "module_label": _env("AUTOGRAPH_MODULE_LABEL") or DEFAULT_MODULE_LABEL,
            "source": "env",
        }

    cached = read_provisioned(persist_path)
    if cached:
        log.info("Found cached deployment: %s", persist_path)
        return {**cached, "source": "cache"}

    if not auto_provision:
        raise SystemExit(
            f"No AUTOGRAPH_API_URL set and no cached deployment at {persist_path}. "
            f"Either:\n"
            f"  - set AUTOGRAPH_API_URL=... in .env to point at an existing AutoGraph, or\n"
            f"  - run `python ingest.py provision` first."
        )

    openai_key = _env("OPENAI_API_KEY")
    if not openai_key:
        raise SystemExit(
            f"No AUTOGRAPH_API_URL, no cached {persist_path}, and no OPENAI_API_KEY "
            f"to auto-provision with.\n"
            f"Either:\n"
            f"  - set AUTOGRAPH_API_URL=... in .env to point at an existing AutoGraph, or\n"
            f"  - set OPENAI_API_KEY=... in .env to auto-provision a new one."
        )

    log.info("No AUTOGRAPH_API_URL or cached deployment found — auto-provisioning ...")
    arango_url, user, password, tls = _arango_creds()
    record = do_provision(
        arango_url=arango_url,
        arango_user=user,
        arango_password=password,
        arango_tls_verify=tls,
        openai_api_key=openai_key,
        db_name=_env("ARANGO_INGEST_DB_NAME") or DEFAULT_DB_NAME,
        project_name=_env("ARANGO_INGEST_PROJECT_NAME") or DEFAULT_PROJECT_NAME,
        module_label=_env("AUTOGRAPH_MODULE_LABEL") or DEFAULT_MODULE_LABEL,
        chat_model=_env("AUTOGRAPH_CHAT_MODEL") or DEFAULT_CHAT_MODEL,
        embedding_model=_env("AUTOGRAPH_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL,
        embedding_dim=int(_env("AUTOGRAPH_EMBEDDING_DIM") or DEFAULT_EMBEDDING_DIM),
        persist_path=persist_path,
    )
    return {**record, "source": "auto_provisioned"}


def build_client(autograph_api_url: str) -> AutographClient:
    arango_url, user, password, tls = _arango_creds()
    return AutographClient(
        api_url=autograph_api_url,
        arango_url=arango_url,
        user=user,
        password=password,
        tls_verify=tls,
    )


# ---------------------------------------------------------------------------
# markdown helpers
# ---------------------------------------------------------------------------


def collect_md_files(md_dir: Path) -> list[Path]:
    files = sorted(p for p in md_dir.iterdir() if p.is_file() and p.suffix.lower() == ".md")
    if not files:
        raise SystemExit(f"No .md files found in {md_dir}")
    return files


def maybe_convert(source_dir: Path | None, md_dir: Path, force: bool) -> None:
    if source_dir is None:
        return
    if not source_dir.is_dir():
        raise SystemExit(f"--source-dir not found: {source_dir}")
    log.info("Converting %s -> %s ...", source_dir, md_dir)
    results = convert_directory(source_dir, md_dir, force=force)
    write_log(results, md_dir.parent / "markdown_conversion_log.csv")
    failed = [r for r in results if r.status == "failed"]
    if failed:
        for r in failed:
            log.error("FAILED to convert %s: %s", r.input_path.name, r.error_msg)
        raise SystemExit(f"{len(failed)} file(s) failed to convert; aborting.")
    log.info("Conversion done: %d file(s) ready in %s", len(results), md_dir)


# ---------------------------------------------------------------------------
# ingestion phases
# ---------------------------------------------------------------------------


def phase_a_import(client: AutographClient, files: list[Path], module: str) -> None:
    log.info("=== Phase A: import-multiple (%d files) ===", len(files))
    t0 = time.perf_counter()
    specs = [FileSpec.from_path(p) for p in files]
    total_bytes = sum(len(s.md_bytes) for s in specs)
    result = client.import_multiple(specs, module=module)
    elapsed = time.perf_counter() - t0
    log.info(
        "Phase A done in %.1fs: %d files, %d bytes, %d batch(es)",
        elapsed, result.n_files, total_bytes, len(result.batches),
    )


def phase_b_corpus_build(
    client: AutographClient,
    module: str,
    top_k: int,
    cluster_threshold: int,
    poll_interval_s: float,
    timeout_s: float,
) -> None:
    log.info(
        "=== Phase B: corpus build (module=%s, top_k=%d, cluster_threshold=%d) ===",
        module, top_k, cluster_threshold,
    )
    t0 = time.perf_counter()
    kickoff, kickoff_ms = client.create_corpus_build(
        modules=[module],
        incremental=False,
        top_k=top_k,
        cluster_threshold=cluster_threshold,
    )
    build_id = (
        kickoff.get("corpusBuildId")
        or kickoff.get("corpus_build_id")
        or kickoff.get("buildId")
        or kickoff.get("build_id")
        or kickoff.get("id")
    )
    if not build_id:
        raise AutographError(f"corpus build kickoff missing build id: {kickoff}")
    log.info(
        "Phase B kicked off in %.0fms (build_id=%s) — polling every %.0fs ...",
        kickoff_ms, build_id, poll_interval_s,
    )

    n_polls = 0
    final = None
    for body, _latency_ms in client.poll_corpus_build(
        str(build_id),
        interval_s=poll_interval_s,
        timeout_s=timeout_s,
    ):
        n_polls += 1
        status = str(body.get("status") or body.get("state") or "").lower()
        progress = body.get("progress")
        msg = (body.get("message") or "")[:120]
        log.info(
            "Phase B poll #%d status=%s progress=%s msg=%s",
            n_polls, status, progress, msg,
        )
        if status in {"completed", "failed", "error", "cancelled"}:
            final = body
            break

    elapsed = time.perf_counter() - t0
    if not final or str(final.get("status") or "").lower() != "completed":
        raise AutographError(
            f"corpus build {build_id} ended in non-completed state: "
            f"{(final or {}).get('status')!r}"
        )
    log.info("Phase B done in %.1fs (%d polls)", elapsed, n_polls)


def phase_c_strategizer(
    client: AutographClient,
    poll_interval_s: float,
    timeout_s: float,
) -> dict:
    log.info("=== Phase C: rag-strategizer ===")
    t0 = time.perf_counter()
    _analyze, analyze_ms = client.analyze_strategizer()
    log.info(
        "Phase C analyze done in %.0fms — polling /strategy until stable ...",
        analyze_ms,
    )
    final, wait_ms, history = client.wait_for_strategy_stable(
        interval_s=poll_interval_s,
        timeout_s=timeout_s,
    )
    elapsed = time.perf_counter() - t0
    n_strategies = len(final.get("strategies") or [])
    log.info(
        "Phase C done in %.1fs (%d polls, %d strategies, stabilized after %.0fms)",
        elapsed, len(history), n_strategies, wait_ms,
    )
    return final


def phase_d_orchestrate(
    client: AutographClient,
    replicas: int,
    wait_for_prior_s: float,
) -> None:
    log.info("=== Phase D: orchestrate (replicas=%d) ===", replicas)
    t0 = time.perf_counter()
    response, kickoff_ms, prior_wait_ms = client.orchestrate_with_wait(
        replicas=replicas,
        wait_for_prior_s=wait_for_prior_s,
    )
    if response.get("success") is False:
        raise AutographError(f"orchestrate kickoff returned success=false: {response}")
    log.info(
        "Phase D kicked off in %.0fms (waited %.1fs for prior orchestration to clear)",
        kickoff_ms, prior_wait_ms / 1000.0,
    )
    log.info(
        "  total_jobs=%s completed=%s failed=%s",
        response.get("total_jobs"),
        response.get("completed_jobs"),
        response.get("failed_jobs"),
    )
    elapsed = time.perf_counter() - t0
    log.info("Phase D kickoff done in %.1fs.", elapsed)
    log.info(
        "NOTE: orchestrate kickoff returns immediately. The graph keeps "
        "materializing in the background. Verify completion by querying "
        "ArangoDB collection counts via the AI Suite UI or the /_db/<DB>/_api "
        "endpoints. Typical wall time: 3-15 min depending on corpus size."
    )


# ---------------------------------------------------------------------------
# subcommand: ingest
# ---------------------------------------------------------------------------


def cmd_ingest(args: argparse.Namespace) -> int:
    persist_path: Path = args.persist_path
    md_dir: Path = args.md_dir.resolve()

    if args.source_dir and not args.skip_convert:
        md_dir.mkdir(parents=True, exist_ok=True)
        maybe_convert(args.source_dir.resolve(), md_dir, args.force_convert)

    if not md_dir.is_dir():
        raise SystemExit(f"--md-dir not found: {md_dir}")

    files = collect_md_files(md_dir)
    log.info("Found %d markdown file(s) in %s", len(files), md_dir)

    target = resolve_autograph_target(
        persist_path=persist_path,
        auto_provision=not args.no_auto_provision,
    )
    api_url = target["autograph_api_url"]
    module = (
        args.module
        or target.get("module_label")
        or DEFAULT_MODULE_LABEL
    )

    log.info("AutoGraph URL: %s   (source: %s)", api_url, target["source"])
    log.info("Module label : %s", module)

    client = build_client(api_url)
    health, _ = client.health()
    log.info("Health: %s", health)

    overall_t0 = time.perf_counter()

    if not args.skip_import:
        phase_a_import(client, files, module)
    else:
        log.info("--skip-import: skipping Phase A.")

    if not args.skip_corpus_build:
        phase_b_corpus_build(
            client, module, args.top_k, args.cluster_threshold,
            args.poll_interval_s, args.corpus_timeout_s,
        )
    else:
        log.info("--skip-corpus-build: skipping Phase B.")

    if not args.skip_strategizer:
        phase_c_strategizer(client, args.poll_interval_s, args.strategy_timeout_s)
    else:
        log.info("--skip-strategizer: skipping Phase C.")

    if not args.skip_orchestrate:
        phase_d_orchestrate(client, args.replicas, args.orchestrate_prior_wait_s)
    else:
        log.info("--skip-orchestrate: skipping Phase D.")

    elapsed = time.perf_counter() - overall_t0
    log.info("All done in %.1fs.", elapsed)
    print(f"Ingestion finished in {elapsed:.1f}s.")
    return 0


# ---------------------------------------------------------------------------
# subcommand: provision
# ---------------------------------------------------------------------------


def cmd_provision(args: argparse.Namespace) -> int:
    arango_url, user, password, tls = _arango_creds()
    openai_key = _required_env("OPENAI_API_KEY")
    record = do_provision(
        arango_url=arango_url,
        arango_user=user,
        arango_password=password,
        arango_tls_verify=tls,
        openai_api_key=openai_key,
        db_name=args.db_name,
        project_name=args.project_name,
        module_label=args.module_label,
        chat_model=args.chat_model,
        embedding_model=args.embedding_model,
        embedding_dim=args.embedding_dim,
        explicit_api_url=args.api_url,
        persist_path=args.persist_path,
    )
    print()
    print("Provisioned AutoGraph deployment:")
    print(f"  service_id        = {record['service_id']}")
    print(f"  autograph_api_url = {record['autograph_api_url']}")
    print(f"  db_name           = {record['db_name']}")
    print(f"  project_name      = {record['project_name']}")
    print(f"  module_label      = {record['module_label']}")
    print()
    print(f"State persisted to {args.persist_path}.")
    print("Subsequent `ingest` runs will reuse this automatically.")
    return 0


# ---------------------------------------------------------------------------
# subcommand: teardown
# ---------------------------------------------------------------------------


def cmd_teardown(args: argparse.Namespace) -> int:
    persist_path: Path = args.persist_path
    record = read_provisioned(persist_path)
    if not record:
        raise SystemExit(
            f"No provisioned-service record at {persist_path}. Nothing to tear down."
        )

    if not args.yes:
        print(
            f"Will DELETE service={record['service_id']}, "
            f"project={record['db_name']}/{record['project_name']}"
            f"{', database=' + record['db_name'] if not args.keep_db else ' (DB kept)'}.\n"
            f"Pass --yes to actually run."
        )
        return 1

    arango_url, user, password, tls = _arango_creds()
    summary = do_teardown(
        arango_url=arango_url,
        arango_user=user,
        arango_password=password,
        arango_tls_verify=tls,
        keep_db=args.keep_db,
        persist_path=persist_path,
    )
    print()
    print("Teardown complete:")
    for k, v in summary.items():
        print(f"  {k:20s} = {v}")
    return 0


# ---------------------------------------------------------------------------
# subcommand: status
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    persist_path: Path = args.persist_path
    explicit_url = _env("AUTOGRAPH_API_URL")
    cached = read_provisioned(persist_path)

    print("auto-ingest status")
    print("==================")
    print(f"  AUTOGRAPH_API_URL env :  {explicit_url or '(unset)'}")
    print(f"  Cached state file     :  {persist_path} ({'present' if cached else 'absent'})")
    if cached:
        print(f"     service_id        :  {cached.get('service_id')}")
        print(f"     autograph_api_url :  {cached.get('autograph_api_url')}")
        print(f"     db_name           :  {cached.get('db_name')}")
        print(f"     project_name      :  {cached.get('project_name')}")
        print(f"     module_label     :  {cached.get('module_label')}")
        print(f"     deployed_at       :  {cached.get('deployed_at')}")

    print()
    if explicit_url:
        print("=> ingest will use AUTOGRAPH_API_URL (env var).")
    elif cached:
        print(f"=> ingest will use the cached deployment at {persist_path}.")
    elif _env("OPENAI_API_KEY"):
        print("=> ingest will auto-provision a fresh deployment using OPENAI_API_KEY.")
    else:
        print(
            "=> ingest will FAIL — set either AUTOGRAPH_API_URL or OPENAI_API_KEY in .env."
        )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ingest.py",
        description="Push markdown into AutoGraph (and optionally provision the service).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--persist-path",
        type=Path,
        default=DEFAULT_PROVISIONED_FILE,
        help=f"Path to the provisioned-service JSON cache (default: {DEFAULT_PROVISIONED_FILE}).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ----- ingest -----
    p_ing = sub.add_parser("ingest", help="Run Phase A-D ingestion (auto-detects target).")
    p_ing.add_argument("--source-dir", type=Path, default=None,
                       help="Optional: convert source docs (PDF/DOCX/PPTX/TXT) to .md first.")
    p_ing.add_argument("--md-dir", type=Path, required=True,
                       help="Directory of .md files to ingest. Conversion target if --source-dir is set.")
    p_ing.add_argument("--module", default=None,
                       help="Module label override. Default: cached/.env value or 'auto_ingest'.")
    p_ing.add_argument("--top-k", type=int, default=7)
    p_ing.add_argument("--cluster-threshold", type=int, default=2)
    p_ing.add_argument("--replicas", type=int, default=1)
    p_ing.add_argument("--poll-interval-s", type=float, default=10.0)
    p_ing.add_argument("--corpus-timeout-s", type=float, default=14400.0)
    p_ing.add_argument("--strategy-timeout-s", type=float, default=1800.0)
    p_ing.add_argument("--orchestrate-prior-wait-s", type=float, default=1800.0)
    p_ing.add_argument("--skip-convert", action="store_true")
    p_ing.add_argument("--skip-import", action="store_true")
    p_ing.add_argument("--skip-corpus-build", action="store_true")
    p_ing.add_argument("--skip-strategizer", action="store_true")
    p_ing.add_argument("--skip-orchestrate", action="store_true")
    p_ing.add_argument("--force-convert", action="store_true")
    p_ing.add_argument(
        "--no-auto-provision", action="store_true",
        help="Refuse to auto-provision; require AUTOGRAPH_API_URL or a cached deployment.",
    )
    p_ing.set_defaults(func=cmd_ingest)

    # ----- provision -----
    p_prov = sub.add_parser("provision", help="Deploy a fresh AutoGraph service.")
    p_prov.add_argument("--db-name", default=os.environ.get("ARANGO_INGEST_DB_NAME", DEFAULT_DB_NAME))
    p_prov.add_argument("--project-name", default=os.environ.get("ARANGO_INGEST_PROJECT_NAME", DEFAULT_PROJECT_NAME))
    p_prov.add_argument("--module-label", default=os.environ.get("AUTOGRAPH_MODULE_LABEL", DEFAULT_MODULE_LABEL))
    p_prov.add_argument("--chat-model", default=os.environ.get("AUTOGRAPH_CHAT_MODEL", DEFAULT_CHAT_MODEL))
    p_prov.add_argument("--embedding-model", default=os.environ.get("AUTOGRAPH_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL))
    p_prov.add_argument(
        "--embedding-dim",
        type=int,
        default=int(os.environ.get("AUTOGRAPH_EMBEDDING_DIM", DEFAULT_EMBEDDING_DIM)),
    )
    p_prov.add_argument(
        "--api-url", default=None,
        help=(
            "Skip URL auto-discovery and use this AutoGraph URL directly. "
            "Useful when the AI Suite UI already shows the URL and you don't "
            "want to wait for route propagation. Format: "
            "https://<host>.rnd.pilot.arango.ai/autograph/<5-char-suffix>"
        ),
    )
    p_prov.set_defaults(func=cmd_provision)

    # ----- teardown -----
    p_tear = sub.add_parser("teardown", help="Delete the provisioned service/project/db.")
    p_tear.add_argument("--keep-db", action="store_true",
                        help="Delete the service+project but keep the database for inspection.")
    p_tear.add_argument("--yes", action="store_true",
                        help="Confirm. Without this, teardown is a dry-run.")
    p_tear.set_defaults(func=cmd_teardown)

    # ----- status -----
    p_stat = sub.add_parser("status", help="Print which mode the next ingest will use.")
    p_stat.set_defaults(func=cmd_status)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv(override=False)
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AutographError as exc:
        logging.error("AutoGraph API error: %s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        logging.warning("Interrupted by user.")
        sys.exit(130)
