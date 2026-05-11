"""End-to-end AutoGraph ingestion driver.

Pushes a directory of Markdown files through the four AutoGraph ingestion
phases against an existing AutoGraph deployment:

    Phase A: POST /v1/import-multiple                 (upload)
    Phase B: POST /v1/corpus/builds + GET poll        (chunk, embed, cluster)
    Phase C: POST /v1/rag-strategizer/analyze + poll  (assign RAG strategies)
    Phase D: POST /v1/orchestrate                     (materialize the graph)

Optionally converts source documents (PDF/DOCX/PPTX/TXT) to Markdown first
via `markdown_convert.py`.

Usage:
    # already have markdown
    python ingest.py --md-dir ./md-output -v

    # convert first, then ingest
    python ingest.py --source-dir ./source-docs --md-dir ./md-output -v

    # tune corpus build
    python ingest.py --md-dir ./md-output --top-k 5 --cluster-threshold 3 -v
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

log = logging.getLogger("auto_ingest")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"Missing required env var: {name}. Copy .env.example to .env and fill it in."
        )
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def build_client() -> AutographClient:
    """Read env vars and construct an authenticated AutographClient."""
    api_url = _required_env("AUTOGRAPH_API_URL").rstrip("/")
    arango_url = _required_env("ARANGO_URL").rstrip("/")
    user = _required_env("ARANGO_USER")
    password = _required_env("ARANGO_PASSWORD")
    tls_verify = _bool_env("ARANGO_TLS_VERIFY", default=False)
    return AutographClient(
        api_url=api_url,
        arango_url=arango_url,
        user=user,
        password=password,
        tls_verify=tls_verify,
    )


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


def phase_a_import(
    client: AutographClient,
    files: list[Path],
    module: str,
) -> None:
    log.info("=== Phase A: import-multiple (%d files) ===", len(files))
    t0 = time.perf_counter()
    specs = [FileSpec.from_path(p) for p in files]
    total_bytes = sum(len(s.md_bytes) for s in specs)
    result = client.import_multiple(specs, module=module)
    elapsed = time.perf_counter() - t0
    log.info(
        "Phase A done in %.1fs: %d files, %d bytes, %d batch(es)",
        elapsed,
        result.n_files,
        total_bytes,
        len(result.batches),
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
        module,
        top_k,
        cluster_threshold,
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
    log.info("Phase B kicked off in %.0fms (build_id=%s) — polling every %.0fs ...",
             kickoff_ms, build_id, poll_interval_s)

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
        log.info("Phase B poll #%d status=%s progress=%s msg=%s",
                 n_polls, status, progress, msg)
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
    log.info("Phase C analyze done in %.0fms — polling /strategy until stable ...",
             analyze_ms)
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Push markdown files through the AutoGraph ingestion API."
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="Optional: directory of source docs (PDF/DOCX/PPTX/TXT) to convert "
             "to .md first. Conversion output goes to --md-dir.",
    )
    parser.add_argument(
        "--md-dir",
        type=Path,
        required=True,
        help="Directory of .md files to ingest. If --source-dir is set, this is "
             "the conversion output dir (will be created).",
    )
    parser.add_argument(
        "--module",
        default=os.environ.get("AUTOGRAPH_MODULE_LABEL", "wtw_ingest"),
        help="Module label to tag uploaded docs with. AutoGraph uses this to "
             "scope corpus builds and orchestration. Default: env "
             "AUTOGRAPH_MODULE_LABEL or 'wtw_ingest'.",
    )
    parser.add_argument("--top-k", type=int, default=7,
                        help="Corpus-build top_k (default 7).")
    parser.add_argument("--cluster-threshold", type=int, default=2,
                        help="Corpus-build cluster_threshold (default 2).")
    parser.add_argument("--replicas", type=int, default=1,
                        help="Orchestrate worker replicas (default 1).")

    parser.add_argument("--poll-interval-s", type=float, default=10.0,
                        help="Polling interval for Phase B / C (default 10s).")
    parser.add_argument("--corpus-timeout-s", type=float, default=14400.0,
                        help="Phase B max wall time (default 4h).")
    parser.add_argument("--strategy-timeout-s", type=float, default=1800.0,
                        help="Phase C max wall time (default 30 min).")
    parser.add_argument("--orchestrate-prior-wait-s", type=float, default=1800.0,
                        help="Phase D: max wait for a prior orchestration to clear "
                             "(default 30 min).")

    parser.add_argument("--skip-convert", action="store_true",
                        help="Ignore --source-dir even if set; ingest --md-dir as-is.")
    parser.add_argument("--skip-import", action="store_true",
                        help="Skip Phase A. Useful if docs are already imported.")
    parser.add_argument("--skip-corpus-build", action="store_true",
                        help="Skip Phase B.")
    parser.add_argument("--skip-strategizer", action="store_true",
                        help="Skip Phase C.")
    parser.add_argument("--skip-orchestrate", action="store_true",
                        help="Skip Phase D.")
    parser.add_argument("--force-convert", action="store_true",
                        help="Reconvert source docs even if a fresh .md exists.")

    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    load_dotenv(override=False)

    md_dir: Path = args.md_dir.resolve()

    if args.source_dir and not args.skip_convert:
        md_dir.mkdir(parents=True, exist_ok=True)
        maybe_convert(args.source_dir.resolve(), md_dir, args.force_convert)

    if not md_dir.is_dir():
        raise SystemExit(f"--md-dir not found: {md_dir}")

    files = collect_md_files(md_dir)
    log.info("Found %d markdown file(s) in %s", len(files), md_dir)

    client = build_client()
    log.info("AutoGraph URL: %s", client.api_url)
    log.info("Module label : %s", args.module)
    health, _ = client.health()
    log.info("Health: %s", health)

    overall_t0 = time.perf_counter()

    if not args.skip_import:
        phase_a_import(client, files, args.module)
    else:
        log.info("--skip-import: skipping Phase A.")

    if not args.skip_corpus_build:
        phase_b_corpus_build(
            client, args.module, args.top_k, args.cluster_threshold,
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


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AutographError as exc:
        logging.error("AutoGraph API error: %s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        logging.warning("Interrupted by user.")
        sys.exit(130)
