"""Azure DevOps Server REST client (PAT auth)."""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Iterator

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

    def _post(self, path: str, body: dict, **params) -> dict:
        params.setdefault("api-version", self.s.ado_api_version)
        r = self.http.post(path, params=params, json=body)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------ work items
    def wiql_ids(self, where: str) -> list[int]:
        """Work item ids matching a WIQL WHERE clause (the server caps one query at 20,000 results)."""
        q = f"SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project AND {where} ORDER BY [System.Id]"
        return [w["id"] for w in self._post("wit/wiql", {"query": q}).get("workItems", [])]

    def work_items(self, ids: list[int]) -> Iterator[dict]:
        """Full work items with relations, fetched in batches of 200. Deleted/inaccessible ids are skipped."""
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            data = self._get("wit/workitems", ids=",".join(map(str, chunk)), **{"$expand": "relations",
                                                                              "errorPolicy": "omit"})
            for w in data.get("value", []):
                if w:  # errorPolicy=omit returns null for missing ids
                    yield w

    # ------------------------------------------------------------ pull requests
    def pull_requests(self, repo_id: str, created_after: str) -> Iterator[dict]:
        """Completed PRs of one repo, newest first, until one was created before `created_after` (ISO date)."""
        skip = 0
        while True:
            page = self._get(f"git/repositories/{repo_id}/pullrequests",
                             **{"searchCriteria.status": "completed", "$top": 100, "$skip": skip}).get("value", [])
            for pr in page:
                if pr.get("creationDate", "") < created_after:
                    return
                yield pr
            if len(page) < 100:
                return
            skip += 100

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
