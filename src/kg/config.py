"""Settings read from KG_* environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"KG_{name}", default)


def _bool(name: str, default: bool) -> bool:
    v = _env(name)
    return default if v == "" else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Azure DevOps Server, e.g. https://tfs.acme.local/tfs/DefaultCollection
    ado_url: str = field(default_factory=lambda: _env("ADO_URL").rstrip("/"))
    ado_project: str = field(default_factory=lambda: _env("ADO_PROJECT"))
    ado_pat: str = field(default_factory=lambda: _env("ADO_PAT"))
    # 5.0 works on Azure DevOps Server 2019 and newer.
    ado_api_version: str = field(default_factory=lambda: _env("ADO_API_VERSION", "5.0"))
    # Regexes on repo names (empty = no filter).
    repo_include: str = field(default_factory=lambda: _env("REPO_INCLUDE"))
    repo_exclude: str = field(default_factory=lambda: _env("REPO_EXCLUDE"))

    # TLS for on-prem endpoints: a CA bundle path, or disable verification (not recommended).
    ca_bundle: str = field(default_factory=lambda: _env("CA_BUNDLE"))
    verify_ssl: bool = field(default_factory=lambda: _bool("VERIFY_SSL", True))

    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")))

    neo4j_uri: str = field(default_factory=lambda: _env("NEO4J_URI", "bolt://localhost:7687"))
    neo4j_user: str = field(default_factory=lambda: _env("NEO4J_USER", "neo4j"))
    neo4j_password: str = field(default_factory=lambda: _env("NEO4J_PASSWORD", "neo4j"))
    neo4j_database: str = field(default_factory=lambda: _env("NEO4J_DATABASE", "neo4j"))

    mcp_host: str = field(default_factory=lambda: _env("MCP_HOST", "0.0.0.0"))
    mcp_port: int = field(default_factory=lambda: int(_env("MCP_PORT", "8000")))

    @property
    def mirrors_dir(self) -> Path:
        return self.data_dir / "mirrors"

    @property
    def cache_path(self) -> Path:
        return self.data_dir / "parse_cache.sqlite"

    @property
    def tls_verify(self) -> bool | str:
        if self.ca_bundle:
            return self.ca_bundle
        return self.verify_ssl
