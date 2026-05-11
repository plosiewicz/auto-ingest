"""Provision and teardown an isolated AutoGraph deployment.

Wraps the ACP client to:

- create a database (idempotent),
- create a GenAI project (idempotent),
- deploy a new AutoGraph service against (db, project),
- probe the service's public URL until /v1/health is reachable,
- persist the resulting (service_id, api_url, db, project) to a local JSON
  file so subsequent ``ingest`` runs can reuse it.

The persistence file (default: ``./provisioned_service.json``) is the
"memory" that lets the auto-detect logic in ``ingest.py`` skip provisioning
on subsequent runs.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from acp_client import ACPClient, ACPError, normalize_service_id
from autograph_client import AutographClient

log = logging.getLogger(__name__)

DEFAULT_PROVISIONED_FILE = Path("provisioned_service.json")

DEFAULT_DB_NAME = "auto_ingest_db"
DEFAULT_PROJECT_NAME = "auto_ingest"
DEFAULT_MODULE_LABEL = "auto_ingest"

DEFAULT_CHAT_API_URL = "https://api.openai.com/v1"
DEFAULT_EMBED_API_URL = "https://api.openai.com/v1"
DEFAULT_CHAT_MODEL = "gpt-4.1"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIM = 512

URL_CANDIDATES = [
    "{base}/autograph/{suffix}",
    "{base}/autograph/{full_id}",
    "{base}/_platform/{full_id}",
    "{base}/services/{full_id}",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_provisioned(path: Path = DEFAULT_PROVISIONED_FILE) -> dict | None:
    """Return the parsed provisioned-service JSON, or None if absent/invalid."""
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        if not data.get("service_id") or not data.get("autograph_api_url"):
            return None
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s: %s — treating as missing.", path, exc)
        return None


def write_provisioned(record: dict, path: Path = DEFAULT_PROVISIONED_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    log.info("Wrote %s", path)


def _autograph_service_for(
    acp: ACPClient,
    db_name: str,
    project_name: str,
) -> dict | None:
    """Find an existing AutoGraph deployment matching (db_name, project_name)."""
    services = acp.list_services()
    for s in services:
        sid = normalize_service_id(s) or ""
        if not sid.startswith("arangodb-autograph-"):
            continue
        if (
            s.get("dbName") == db_name
            and s.get("genaiProjectName") == project_name
        ):
            return s
    return None


def resolve_autograph_url(
    arango_url: str,
    arango_user: str,
    arango_password: str,
    arango_tls_verify: bool,
    service_id: str,
    *,
    deadline_s: float = 180.0,
) -> str:
    """Probe URL candidates until one returns 200 on /v1/health.

    Returns the first URL whose ``/v1/health`` endpoint returns 200,
    raising ``RuntimeError`` after ``deadline_s`` if none succeed.

    Default deadline is 180s because AI Suite route propagation can take
    ~90s on a fresh deploy: the URL is correct on the first probe candidate
    but the health endpoint keeps returning 404 until the route table
    updates. If you already know the URL (e.g. you can read it from the
    AI Suite UI's GenAI Services list), bypass this entire function by
    passing ``provision --api-url <url>`` to skip discovery.
    """
    if not service_id.startswith("arangodb-autograph-"):
        raise RuntimeError(
            f"Unexpected service id shape: {service_id!r} (expected arangodb-autograph-<suffix>)"
        )
    suffix = service_id.replace("arangodb-autograph-", "", 1)
    base = arango_url.rstrip("/")
    candidates = [
        c.format(base=base, suffix=suffix, full_id=service_id)
        for c in URL_CANDIDATES
    ]

    deadline = time.monotonic() + deadline_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        for url in candidates:
            try:
                test_client = AutographClient(
                    url,
                    arango_url,
                    arango_user,
                    arango_password,
                    tls_verify=arango_tls_verify,
                    timeout_s=10.0,
                )
                body, latency_ms = test_client.health()
                log.info(
                    "AutoGraph URL resolved: %s -> %s (%.0fms)",
                    url, body, latency_ms,
                )
                return url
            except Exception as e:  # noqa: BLE001
                last_error = e
                log.debug("URL probe failed: %s -> %s", url, e)
        time.sleep(2)
    raise RuntimeError(
        f"Could not resolve AutoGraph URL within {deadline_s}s. Last error: {last_error}\n"
        f"Tried:\n  " + "\n  ".join(candidates)
        + f"\nServiceId: {service_id}"
        + "\nFix: paste the AutoGraph URL from the AI Suite UI's GenAI Services list "
        + "into AUTOGRAPH_API_URL in .env and re-run."
    )


def provision(
    *,
    arango_url: str,
    arango_user: str,
    arango_password: str,
    arango_tls_verify: bool,
    openai_api_key: str,
    db_name: str = DEFAULT_DB_NAME,
    project_name: str = DEFAULT_PROJECT_NAME,
    module_label: str = DEFAULT_MODULE_LABEL,
    chat_model: str = DEFAULT_CHAT_MODEL,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    embedding_dim: int = DEFAULT_EMBEDDING_DIM,
    explicit_api_url: str | None = None,
    persist_path: Path = DEFAULT_PROVISIONED_FILE,
) -> dict:
    """Deploy a fresh AutoGraph service end-to-end.

    Steps:
      1. ACP /health
      2. create_database(db_name)         (idempotent)
      3. create_project(db_name, project) (idempotent)
      4. reuse existing AutoGraph for (db, project) if one matches our labels;
         otherwise deploy_autograph(...) with chat/embedding config
      5. wait_for_service_ready(...) until DEPLOYED
      6. probe AutoGraph public URL (or use explicit_api_url to skip)
      7. write provisioned_service.json next to the script

    Returns the persisted record dict.
    """
    acp = ACPClient(
        arango_url, arango_user, arango_password,
        tls_verify=arango_tls_verify,
    )

    log.info("ACP /health ...")
    log.info("  %s", acp.health())

    log.info("Ensuring database %s exists ...", db_name)
    acp.create_database(db_name)

    log.info("Ensuring project %s/%s exists ...", db_name, project_name)
    acp.create_project(
        db_name, project_name,
        description=f"{project_name} (created by auto-ingest)",
    )

    log.info("Looking for an existing AutoGraph for %s/%s ...", db_name, project_name)
    existing = _autograph_service_for(acp, db_name, project_name)
    if existing:
        sid = normalize_service_id(existing)
        log.info("Reusing existing AutoGraph deployment: %s", sid)
        service_info = existing
    else:
        env = {
            "db_name": db_name,
            "genai_project_name": project_name,
            "chat_api_provider": "openai",
            "chat_api_url": DEFAULT_CHAT_API_URL,
            "chat_api_key": openai_api_key,
            "chat_model": chat_model,
            "embedding_api_provider": "openai",
            "embedding_api_url": DEFAULT_EMBED_API_URL,
            "embedding_api_key": openai_api_key,
            "embedding_model": embedding_model,
            "embedding_dim": str(embedding_dim),
        }
        labels = {
            "deployed_by": "auto-ingest",
            "db_name": db_name,
            "project_name": project_name,
        }
        log.info("Deploying new AutoGraph service against %s/%s ...", db_name, project_name)
        deploy_resp = acp.deploy_autograph(env=env, labels=labels)
        sid = (
            deploy_resp.get("serviceId")
            or deploy_resp.get("service_id")
            or (deploy_resp.get("serviceInfo", {}) or {}).get("serviceId")
        )
        if not sid:
            raise ACPError(f"deploy_autograph response missing serviceId: {deploy_resp}")
        log.info("Service ID: %s", sid)

        log.info("Waiting for service %s to be DEPLOYED (cap 5 min) ...", sid)
        service_info = acp.wait_for_service_ready(sid, timeout_s=300, poll_interval_s=5)

    sid = normalize_service_id(service_info) or normalize_service_id(
        service_info.get("serviceInfo", {}) if isinstance(service_info, dict) else {}
    )
    if not sid:
        raise ACPError(f"Could not resolve serviceId from {service_info!r}")

    if explicit_api_url:
        api_url = explicit_api_url.rstrip("/")
        log.info("Skipping URL probe — using explicit api_url=%s", api_url)
    else:
        log.info("Probing AutoGraph public URL for service %s (cap 180s) ...", sid)
        api_url = resolve_autograph_url(
            arango_url, arango_user, arango_password,
            arango_tls_verify, sid,
        )
        log.info("AutoGraph URL: %s", api_url)

    record = {
        "service_id": sid,
        "autograph_api_url": api_url,
        "db_name": db_name,
        "project_name": project_name,
        "module_label": module_label,
        "chat_model": chat_model,
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
        "deployed_at": _now_iso(),
    }
    write_provisioned(record, persist_path)
    return record


def teardown(
    *,
    arango_url: str,
    arango_user: str,
    arango_password: str,
    arango_tls_verify: bool,
    keep_db: bool = False,
    persist_path: Path = DEFAULT_PROVISIONED_FILE,
) -> dict:
    """Reverse :func:`provision`. Reads the persisted record, deletes the
    service, project, and (optionally) database, then renames the persistence
    file to ``<name>.YYYYMMDDTHHMMSSZ.json.bak`` for forensics.

    Returns a dict summarizing what was deleted.
    """
    record = read_provisioned(persist_path)
    if not record:
        raise SystemExit(
            f"No provisioned-service file found at {persist_path}. Nothing to tear down."
        )

    sid = record["service_id"]
    db_name = record["db_name"]
    project_name = record["project_name"]
    log.info(
        "Tearing down service=%s, project=%s/%s (keep_db=%s) ...",
        sid, db_name, project_name, keep_db,
    )

    acp = ACPClient(
        arango_url, arango_user, arango_password,
        tls_verify=arango_tls_verify,
    )

    summary: dict[str, Any] = {
        "service_id": sid,
        "db_name": db_name,
        "project_name": project_name,
    }

    try:
        log.info("Deleting AutoGraph service %s ...", sid)
        summary["service_deleted"] = acp.delete_service(sid)
    except ACPError as e:
        log.warning("delete_service failed (continuing): %s", e)
        summary["service_deleted"] = False
        summary["service_error"] = str(e)

    try:
        log.info("Deleting project %s/%s ...", db_name, project_name)
        summary["project_deleted"] = acp.delete_project(db_name, project_name)
    except ACPError as e:
        log.warning("delete_project failed (continuing): %s", e)
        summary["project_deleted"] = False
        summary["project_error"] = str(e)

    if not keep_db:
        try:
            log.info("Deleting database %s ...", db_name)
            summary["database_deleted"] = acp.delete_database(db_name)
        except ACPError as e:
            log.warning("delete_database failed (continuing): %s", e)
            summary["database_deleted"] = False
            summary["database_error"] = str(e)
    else:
        log.info("--keep-db set; leaving database %s intact.", db_name)
        summary["database_deleted"] = False

    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = persist_path.with_suffix(f".{ts}.json.bak")
    persist_path.rename(backup)
    log.info("Moved provisioned record to %s", backup)
    summary["forensic_record"] = str(backup)
    return summary
