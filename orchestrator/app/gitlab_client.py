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

    def merge_request(self, project: int | str, iid: int) -> dict[str, Any]:
        return self.get(f"/projects/{_pid(project)}/merge_requests/{iid}")

    def merge_request_diffs(self, project: int | str, iid: int) -> list[dict[str, Any]]:
        return self.get_all(f"/projects/{_pid(project)}/merge_requests/{iid}/diffs", max_pages=5)

    def merge_requests(self, project: int | str, **params: Any) -> list[dict[str, Any]]:
        return self.get(f"/projects/{_pid(project)}/merge_requests", **params)

    def create_merge_request(self, project: int | str, **fields: Any) -> dict[str, Any]:
        return self.post(f"/projects/{_pid(project)}/merge_requests", fields)

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

    # -- access -------------------------------------------------------------------------
    def member_access_level(self, project: int | str, user_id: int) -> int:
        resp = self.http.get(f"/projects/{_pid(project)}/members/all/{user_id}")
        if resp.status_code == 404:
            return 0
        if resp.status_code >= 400:
            raise GitLabError(resp.status_code, resp.text[:500])
        return int(resp.json().get("access_level", 0))
