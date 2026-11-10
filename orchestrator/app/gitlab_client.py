"""Minimal GitLab REST v4 client (httpx). Only the endpoints the platform uses."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

DEVELOPER = 30


class GitLabError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitLab API {status}: {message}")
        self.status = status


def _pid(project: int | str) -> str:
    return str(project) if isinstance(project, int) or str(project).isdigit() else quote(str(project), safe="")


class GitLab:
    def __init__(self, base_url: str, token: str, verify: str | bool = True, transport: httpx.BaseTransport | None = None):
        self.graphql_url = f"{base_url.rstrip('/')}/api/graphql"
        self.http = httpx.Client(
            base_url=f"{base_url.rstrip('/')}/api/v4",
            headers={"PRIVATE-TOKEN": token},
            verify=verify,
            timeout=httpx.Timeout(60.0, connect=10.0),
            transport=transport,
        )

    def close(self) -> None:
        self.http.close()

    # -- plumbing -----------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        resp = self.http.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise GitLabError(resp.status_code, resp.text[:500])
        return resp

    def get(self, path: str, /, **params: Any) -> Any:
        return self._request("GET", path, params=params or None).json()

    def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        resp = self._request("POST", path, json=json)
        return resp.json() if resp.content else None

    def put(self, path: str, json: dict[str, Any]) -> Any:
        return self._request("PUT", path, json=json).json()

    def get_all(self, path: str, /, max_pages: int = 10, **params: Any) -> list[Any]:
        items: list[Any] = []
        params = {"per_page": 100, **params}
        for page in range(1, max_pages + 1):
            batch = self._request("GET", path, params={**params, "page": page}).json()
            items.extend(batch)
            if len(batch) < params["per_page"]:
                break
        return items

    # -- projects / repository ----------------------------------------------------
    def project(self, project: int | str) -> dict[str, Any]:
        return self.get(f"/projects/{_pid(project)}")

    def raw_file(self, project: int | str, path: str, ref: str) -> str | None:
        resp = self.http.get(f"/projects/{_pid(project)}/repository/files/{quote(path, safe='')}/raw", params={"ref": ref})
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise GitLabError(resp.status_code, resp.text[:500])
        return resp.text

    def archive(self, project: int | str, sha: str, max_bytes: int) -> bytes | None:
        """The repository at `sha` as tar.gz, or None when it is larger than max_bytes."""
        chunks, size = [], 0
        with self.http.stream("GET", f"/projects/{_pid(project)}/repository/archive.tar.gz", params={"sha": sha}) as resp:
            if resp.status_code >= 400:
                raise GitLabError(resp.status_code, resp.read().decode("utf-8", "replace")[:500])
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    return None
                chunks.append(chunk)
        return b"".join(chunks)

    def branch(self, project: int | str, name: str) -> dict[str, Any] | None:
        resp = self.http.get(f"/projects/{_pid(project)}/repository/branches/{quote(name, safe='')}")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise GitLabError(resp.status_code, resp.text[:500])
        return resp.json()

    def repository_tree(self, project: int | str, path: str = "", ref: str | None = None,
                        recursive: bool = False, max_pages: int = 3) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"recursive": str(recursive).lower()}
        if path:
            params["path"] = path
        if ref:
            params["ref"] = ref
        try:
            return self.get_all(f"/projects/{_pid(project)}/repository/tree", max_pages=max_pages, **params)
        except GitLabError as exc:
            if exc.status == 404:  # missing directory or empty repository
                return []
            raise

    def search(self, project: int | str, scope: str, query: str, ref: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"scope": scope, "search": query, "per_page": 50}
        if ref:
            params["ref"] = ref
        return self.get(f"/projects/{_pid(project)}/search", **params)

    def compare(self, project: int | str, from_sha: str, to_sha: str) -> dict[str, Any]:
        return self.get(f"/projects/{_pid(project)}/repository/compare", **{"from": from_sha, "to": to_sha})

    def commit_diff(self, project: int | str, sha: str) -> list[dict[str, Any]]:
        return self.get_all(f"/projects/{_pid(project)}/repository/commits/{sha}/diff", max_pages=3)

    def create_commit(self, project: int | str, branch: str, message: str, actions: list[dict[str, Any]],
                      start_sha: str | None = None, start_branch: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"branch": branch, "commit_message": message, "actions": actions}
        if start_sha:
            body["start_sha"] = start_sha
        if start_branch:
            body["start_branch"] = start_branch
        return self.post(f"/projects/{_pid(project)}/repository/commits", body)

    def create_commit_comment(self, project: int | str, sha: str, note: str) -> Any:
        return self.post(f"/projects/{_pid(project)}/repository/commits/{sha}/comments", {"note": note})

    # -- CI -------------------------------------------------------------------------
    def pipeline(self, project: int | str, pipeline_id: int) -> dict[str, Any]:
        return self.get(f"/projects/{_pid(project)}/pipelines/{pipeline_id}")

    def pipelines(self, project: int | str, **params: Any) -> list[dict[str, Any]]:
        return self.get(f"/projects/{_pid(project)}/pipelines", **params)

    def pipeline_jobs(self, project: int | str, pipeline_id: int, scope: str | None = None) -> list[dict[str, Any]]:
        params = {"scope[]": scope} if scope else {}
        return self.get_all(f"/projects/{_pid(project)}/pipelines/{pipeline_id}/jobs", **params)

    def job_trace(self, project: int | str, job_id: int) -> str:
        return self._request("GET", f"/projects/{_pid(project)}/jobs/{job_id}/trace").text

    def retry_job(self, project: int | str, job_id: int) -> Any:
        return self.post(f"/projects/{_pid(project)}/jobs/{job_id}/retry")

    def ci_lint_ref(self, project: int | str, ref: str) -> dict[str, Any]:
        return self.get(f"/projects/{_pid(project)}/ci/lint", content_ref=ref, include_jobs="false")

    def ci_lint_content(self, project: int | str, content: str, ref: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"content": content}
        if ref:
            body["ref"] = ref
        return self.post(f"/projects/{_pid(project)}/ci/lint", body)

    def cancel_pipeline(self, project: int | str, pipeline_id: int) -> Any:
        return self.post(f"/projects/{_pid(project)}/pipelines/{pipeline_id}/cancel")

    def trigger_pipeline(self, project: int | str, ref: str, variables: dict[str, str]) -> dict[str, Any]:
        return self.post(
            f"/projects/{_pid(project)}/pipeline",
            {"ref": ref, "variables": [{"key": k, "value": v, "variable_type": "env_var"} for k, v in variables.items()]},
        )

    # -- issues / MRs / notes -------------------------------------------------------
    def issue(self, project: int | str, iid: int) -> dict[str, Any]:
        return self.get(f"/projects/{_pid(project)}/issues/{iid}")

    def issues(self, project: int | str, **params: Any) -> list[dict[str, Any]]:
        return self.get(f"/projects/{_pid(project)}/issues", **params)

    def merge_request(self, project: int | str, iid: int, **params: Any) -> dict[str, Any]:
        return self.get(f"/projects/{_pid(project)}/merge_requests/{iid}", **params)

    def rebase_merge_request(self, project: int | str, iid: int) -> Any:
        """GitLab's own rebase of the source branch onto the target (asynchronous; fails on conflicts)."""
        return self.put(f"/projects/{_pid(project)}/merge_requests/{iid}/rebase", {})

    def merge_request_diffs(self, project: int | str, iid: int) -> list[dict[str, Any]]:
        return self.get_all(f"/projects/{_pid(project)}/merge_requests/{iid}/diffs", max_pages=5)

    def merge_requests(self, project: int | str, **params: Any) -> list[dict[str, Any]]:
        return self.get(f"/projects/{_pid(project)}/merge_requests", **params)

    def create_merge_request(self, project: int | str, **fields: Any) -> dict[str, Any]:
        return self.post(f"/projects/{_pid(project)}/merge_requests", fields)

    def update_merge_request(self, project: int | str, iid: int, **fields: Any) -> dict[str, Any]:
        return self.put(f"/projects/{_pid(project)}/merge_requests/{iid}", fields)

    def update_issue(self, project: int | str, iid: int, **fields: Any) -> dict[str, Any]:
        return self.put(f"/projects/{_pid(project)}/issues/{iid}", fields)

    def labels(self, project: int | str) -> list[dict[str, Any]]:
        return self.get_all(f"/projects/{_pid(project)}/labels", max_pages=3)

    def notes(self, project: int | str, noteable: str, iid: int) -> list[dict[str, Any]]:
        kind = "issues" if noteable == "issue" else "merge_requests"
        return self.get_all(f"/projects/{_pid(project)}/{kind}/{iid}/notes", max_pages=3, sort="asc", order_by="created_at")

    def create_note(self, project: int | str, noteable: str, iid: int, body: str) -> dict[str, Any]:
        kind = "issues" if noteable == "issue" else "merge_requests"
        return self.post(f"/projects/{_pid(project)}/{kind}/{iid}/notes", {"body": body})

    def update_note(self, project: int | str, noteable: str, iid: int, note_id: int, body: str) -> dict[str, Any]:
        kind = "issues" if noteable == "issue" else "merge_requests"
        return self.put(f"/projects/{_pid(project)}/{kind}/{iid}/notes/{note_id}", {"body": body})

    def discussions(self, project: int | str, noteable: str, iid: int) -> list[dict[str, Any]]:
        kind = "issues" if noteable == "issue" else "merge_requests"
        return self.get_all(f"/projects/{_pid(project)}/{kind}/{iid}/discussions", max_pages=3)

    def discussion(self, project: int | str, noteable: str, iid: int, discussion_id: str) -> dict[str, Any]:
        kind = "issues" if noteable == "issue" else "merge_requests"
        return self.get(f"/projects/{_pid(project)}/{kind}/{iid}/discussions/{discussion_id}")

    def reply_to_discussion(self, project: int | str, noteable: str, iid: int, discussion_id: str, body: str) -> Any:
        kind = "issues" if noteable == "issue" else "merge_requests"
        return self.post(f"/projects/{_pid(project)}/{kind}/{iid}/discussions/{discussion_id}/notes", {"body": body})

    def create_mr_discussion(self, project: int | str, iid: int, body: str, position: dict[str, Any] | None = None) -> Any:
        payload: dict[str, Any] = {"body": body}
        if position:
            payload["position"] = position
        return self.post(f"/projects/{_pid(project)}/merge_requests/{iid}/discussions", payload)

    def award_note_emoji(self, project: int | str, noteable: str, iid: int, note_id: int, name: str) -> Any:
        kind = "issues" if noteable == "issue" else "merge_requests"
        return self.post(f"/projects/{_pid(project)}/{kind}/{iid}/notes/{note_id}/award_emoji", {"name": name})

    def note_award_emoji(self, project: int | str, noteable: str, iid: int, note_id: int) -> list[dict[str, Any]]:
        kind = "issues" if noteable == "issue" else "merge_requests"
        return self.get_all(f"/projects/{_pid(project)}/{kind}/{iid}/notes/{note_id}/award_emoji", max_pages=2)

    def mr_award_emoji(self, project: int | str, iid: int) -> list[dict[str, Any]]:
        return self.get_all(f"/projects/{_pid(project)}/merge_requests/{iid}/award_emoji", max_pages=2)

    # -- security reports (Ultimate) ------------------------------------------------------
    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run a GraphQL *query*. Mutations are refused: the platform only reads through GraphQL."""
        if query.lstrip().lower().startswith("mutation") or "mutation " in query.lower():
            raise ValueError("GraphQL mutations are not allowed")
        body = self._request("POST", self.graphql_url, json={"query": query, "variables": variables}).json()
        if body.get("errors"):
            raise GitLabError(200, "; ".join(str(e.get("message")) for e in body["errors"])[:500])
        return body.get("data") or {}

    def job_artifact(self, project: int | str, job_id: int, path: str) -> bytes | None:
        resp = self.http.get(f"/projects/{_pid(project)}/jobs/{job_id}/artifacts/{quote(path)}")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise GitLabError(resp.status_code, resp.text[:500])
        return resp.content

    def security_findings(self, project: int | str, pipeline_id: int) -> list[dict[str, Any]]:
        """Security scanner findings of a pipeline, normalised. GraphQL first; report files as a fallback."""
        try:
            return self._findings_graphql(project, pipeline_id)
        except GitLabError:
            return self._findings_artifacts(project, pipeline_id)

    def _findings_graphql(self, project: int | str, pipeline_id: int) -> list[dict[str, Any]]:
        path = self.project(project)["path_with_namespace"]
        iid = self.pipeline(project, pipeline_id).get("iid")
        if iid is None:
            raise GitLabError(404, "pipeline has no iid")
        data = self.graphql(SECURITY_FINDINGS_QUERY, {"path": path, "iid": str(iid)})
        pipeline = ((data.get("project") or {}).get("pipeline")) or {}
        nodes = ((pipeline.get("securityReportFindings") or {}).get("nodes")) or []
        out = []
        for n in nodes:
            loc = n.get("location") or {}
            dep = loc.get("dependency") or {}
            out.append(_finding(
                id=n.get("uuid"), scanner=n.get("reportType"), severity=n.get("severity"), name=n.get("title"),
                description=n.get("description"), solution=n.get("solution"), file=loc.get("file"),
                line=loc.get("startLine"), package=(dep.get("package") or {}).get("name"), version=dep.get("version"),
                identifiers=[i.get("name") or i.get("externalId") for i in n.get("identifiers") or []],
            ))
        return out

    def _findings_artifacts(self, project: int | str, pipeline_id: int) -> list[dict[str, Any]]:
        import json as _json

        out = []
        for job in self.pipeline_jobs(project, pipeline_id):
            types = {a.get("file_type") for a in job.get("artifacts") or []}
            for kind in sorted(types & set(REPORT_FILES)):
                raw = self.job_artifact(project, job["id"], REPORT_FILES[kind])
                if not raw:
                    continue  # the report is only a typed artifact, not a downloadable file
                try:
                    report = _json.loads(raw)
                except ValueError:
                    continue
                for v in report.get("vulnerabilities") or []:
                    loc = v.get("location") or {}
                    dep = loc.get("dependency") or {}
                    out.append(_finding(
                        id=v.get("id"), scanner=kind, severity=v.get("severity"), name=v.get("name") or v.get("message"),
                        description=v.get("description"), solution=v.get("solution"), file=loc.get("file"),
                        line=loc.get("start_line"), package=(dep.get("package") or {}).get("name"),
                        version=dep.get("version"),
                        identifiers=[i.get("name") or i.get("value") for i in v.get("identifiers") or []],
                    ))
        return out

    # -- access -------------------------------------------------------------------------
    def member_access_level(self, project: int | str, user_id: int) -> int:
        resp = self.http.get(f"/projects/{_pid(project)}/members/all/{user_id}")
        if resp.status_code == 404:
            return 0
        if resp.status_code >= 400:
            raise GitLabError(resp.status_code, resp.text[:500])
        return int(resp.json().get("access_level", 0))


REPORT_FILES = {
    "sast": "gl-sast-report.json",
    "dependency_scanning": "gl-dependency-scanning-report.json",
    "secret_detection": "gl-secret-detection-report.json",
    "container_scanning": "gl-container-scanning-report.json",
}

SECURITY_FINDINGS_QUERY = """
query($path: ID!, $iid: ID!) {
  project(fullPath: $path) {
    pipeline(iid: $iid) {
      securityReportFindings(first: 100) {
        nodes {
          uuid title severity reportType description solution
          identifiers { name externalId }
          location {
            ... on VulnerabilityLocationSast { file startLine }
            ... on VulnerabilityLocationSecretDetection { file startLine }
            ... on VulnerabilityLocationDependencyScanning { file dependency { package { name } version } }
          }
        }
      }
    }
  }
}
"""


def _finding(**fields: Any) -> dict[str, Any]:
    out = {k: v for k, v in fields.items() if v not in (None, "", [])}
    out["severity"] = str(out.get("severity") or "unknown").lower()
    out["scanner"] = str(out.get("scanner") or "unknown").lower()
    return out
