"""Self-contained AutoGraph API ingestion package.

Drop-in clone of just the ingestion bits from the wtw-benchmark repo.
No internal cross-imports: `autograph_client.py` and `markdown_convert.py`
are stdlib + (requests, tenacity, urllib3, python-dotenv, markitdown) only.
"""
