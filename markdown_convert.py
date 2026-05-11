"""Convert source documents (PDF / DOCX / PPTX / TXT) to Markdown.

Uses Microsoft's `markitdown` for `.pdf`, `.pptx`, `.docx`. Falls back to
`pandoc` (if installed) when markitdown returns a sub-1KB stub or raises.
Idempotent: skips files whose output `.md` already exists and is newer
than the source. Pass `--force` to reconvert.

Per-file metadata is logged to a CSV (default
``./markdown_conversion_log.csv``) so you can audit which converter ran
on each file and how big the output came out.

CLI:
    python markdown_convert.py -i ./source-docs -o ./md-output [--force] -v
"""

from __future__ import annotations

import argparse
import csv
import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = SCRIPT_DIR / "source-docs"
DEFAULT_OUTPUT = SCRIPT_DIR / "md-output"
DEFAULT_LOG_CSV = SCRIPT_DIR / "markdown_conversion_log.csv"

SUPPORTED_EXTENSIONS = {".pdf", ".pptx", ".docx", ".doc", ".ppt", ".txt", ".md"}


@dataclass
class ConversionResult:
    input_path: Path
    output_path: Path
    status: str
    pre_bytes: int
    post_bytes: int
    conversion_ms: float
    converter: str
    error_msg: str = ""


def _try_markitdown(input_path: Path, output_path: Path) -> tuple[bool, str]:
    """Run markitdown. Returns (ok, error_message)."""
    try:
        from markitdown import MarkItDown
    except ImportError as e:
        return False, f"markitdown not installed: {e}"
    try:
        md = MarkItDown(enable_plugins=False)
        result = md.convert(str(input_path))
        text = result.text_content or ""
        output_path.write_text(text, encoding="utf-8")
        return True, ""
    except Exception as e:
        return False, f"markitdown error: {type(e).__name__}: {e}"


def _try_pandoc(input_path: Path, output_path: Path) -> tuple[bool, str]:
    """Fallback to pandoc if available. Returns (ok, error_message)."""
    if shutil.which("pandoc") is None:
        return False, "pandoc binary not found on PATH (brew install pandoc)"
    try:
        result = subprocess.run(
            [
                "pandoc",
                str(input_path),
                "-t",
                "gfm",
                "-o",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if result.returncode != 0:
            return False, f"pandoc rc={result.returncode}: {result.stderr[:300]}"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "pandoc timed out after 300s"
    except Exception as e:
        return False, f"pandoc error: {type(e).__name__}: {e}"


def convert_one(
    input_path: Path,
    output_dir: Path,
    *,
    force: bool = False,
) -> ConversionResult:
    """Convert one file. Skips if output is fresh enough (unless ``force``)."""
    output_path = output_dir / f"{input_path.stem}.md"
    pre_bytes = input_path.stat().st_size

    if (
        not force
        and output_path.exists()
        and output_path.stat().st_mtime >= input_path.stat().st_mtime
    ):
        post_bytes = output_path.stat().st_size
        log.info(
            "Skipping %s -> %s (already converted, %d bytes)",
            input_path.name,
            output_path.name,
            post_bytes,
        )
        return ConversionResult(
            input_path=input_path,
            output_path=output_path,
            status="skipped",
            pre_bytes=pre_bytes,
            post_bytes=post_bytes,
            conversion_ms=0.0,
            converter="(skip)",
        )

    log.info("Converting %s ...", input_path.name)
    t0 = time.perf_counter()

    ok, err = _try_markitdown(input_path, output_path)
    converter = "markitdown" if ok else ""
    if ok and output_path.exists() and output_path.stat().st_size < 1024:
        log.warning(
            "markitdown produced a thin output (<1KB) for %s — falling back to pandoc.",
            input_path.name,
        )
        ok = False
        err = "markitdown output <1KB (likely failed extraction)"

    if not ok:
        log.warning("markitdown failed for %s: %s", input_path.name, err)
        ok2, err2 = _try_pandoc(input_path, output_path)
        if ok2:
            converter = "pandoc"
            err = ""
        else:
            converter = "(failed)"
            err = f"{err}; pandoc fallback: {err2}"

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    post_bytes = output_path.stat().st_size if output_path.exists() else 0

    if not ok and converter == "(failed)":
        return ConversionResult(
            input_path=input_path,
            output_path=output_path,
            status="failed",
            pre_bytes=pre_bytes,
            post_bytes=post_bytes,
            conversion_ms=elapsed_ms,
            converter=converter,
            error_msg=err,
        )

    if post_bytes < 1024:
        log.warning(
            "Output for %s is only %d bytes — likely incomplete extraction. "
            "Inspect manually before relying on the result.",
            input_path.name,
            post_bytes,
        )

    return ConversionResult(
        input_path=input_path,
        output_path=output_path,
        status="converted",
        pre_bytes=pre_bytes,
        post_bytes=post_bytes,
        conversion_ms=elapsed_ms,
        converter=converter,
    )


def convert_directory(
    input_dir: Path,
    output_dir: Path,
    *,
    force: bool = False,
) -> list[ConversionResult]:
    output_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(
        p
        for p in input_dir.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    if not files:
        log.warning("No supported source files found in %s", input_dir)
        return []

    log.info(
        "Converting %d file(s) from %s -> %s (force=%s)",
        len(files),
        input_dir,
        output_dir,
        force,
    )

    results = []
    for f in files:
        try:
            results.append(convert_one(f, output_dir, force=force))
        except Exception as e:
            log.exception("Unexpected error converting %s", f.name)
            results.append(
                ConversionResult(
                    input_path=f,
                    output_path=output_dir / f"{f.stem}.md",
                    status="failed",
                    pre_bytes=f.stat().st_size,
                    post_bytes=0,
                    conversion_ms=0.0,
                    converter="(crash)",
                    error_msg=f"{type(e).__name__}: {e}",
                )
            )
    return results


def write_log(results: list[ConversionResult], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "input_path",
                "output_path",
                "status",
                "pre_bytes",
                "post_bytes",
                "conversion_ms",
                "converter",
                "error_msg",
            ]
        )
        for r in results:
            writer.writerow(
                [
                    str(r.input_path),
                    str(r.output_path),
                    r.status,
                    r.pre_bytes,
                    r.post_bytes,
                    f"{r.conversion_ms:.1f}",
                    r.converter,
                    r.error_msg,
                ]
            )
    log.info("Wrote conversion log: %s", log_path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert source docs to Markdown.")
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"Input directory (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output directory (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--log-csv",
        type=Path,
        default=DEFAULT_LOG_CSV,
        help=f"Path to conversion log CSV (default: {DEFAULT_LOG_CSV})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reconvert files even if a fresh output exists.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.input.is_dir():
        log.error("Input dir not found: %s", args.input)
        return 2

    results = convert_directory(args.input, args.output, force=args.force)
    write_log(results, args.log_csv)

    converted = sum(1 for r in results if r.status == "converted")
    skipped = sum(1 for r in results if r.status == "skipped")
    failed = sum(1 for r in results if r.status == "failed")
    print(
        f"Conversion done: {converted} converted, {skipped} skipped, {failed} failed."
    )
    if failed:
        for r in results:
            if r.status == "failed":
                print(f"  FAILED: {r.input_path.name} — {r.error_msg}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
