"""Azure DevOps Server REST client (PAT auth)."""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass

import httpx

from kg.config import Settings


@dataclass(frozen=True)
class AdoRepo:
    id: str
    name: str
    remote_url: str
    default_branch: str  # e.g. "main" (without refs/heads/)


def basic_auth_header(pat: str) -> str:
    return "Basic " + base64.b64encode(f":{pat}".encode()).decode()


class AdoClient:
    def __init__(self, settings: Settings) -> None:
        if not (settings.ado_url and settings.ado_project and settings.ado_pat):
            raise ValueError("KG_ADO_URL, KG_ADO_PROJECT and KG_ADO_PAT must be set")
        self.s = settings
        self.http = httpx.Client(
            base_url=f"{settings.ado_url}/{settings.ado_project}/_apis/",
            headers={"Authorization": basic_auth_header(settings.ado_pat), "Accept": "application/json"},
            verify=settings.tls_verify,
            timeout=60,
        )

    def _get(self, path: str, **params) -> dict:
        params.setdefault("api-version", self.s.ado_api_version)
        r = self.http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def list_repos(self) -> list[AdoRepo]:
        inc = re.compile(self.s.repo_include) if self.s.repo_include else None
        exc = re.compile(self.s.repo_exclude) if self.s.repo_exclude else None
        repos = []
        for r in self._get("git/repositories")["value"]:
            name = r["name"]
            if r.get("isDisabled") or not r.get("defaultBranch"):
                continue  # disabled or empty repos have nothing to index
            if (inc and not inc.search(name)) or (exc and exc.search(name)):
                continue
            repos.append(AdoRepo(
                id=r["id"], name=name, remote_url=r["remoteUrl"],
                default_branch=r["defaultBranch"].removeprefix("refs/heads/"),
            ))
        return sorted(repos, key=lambda x: x.name)
