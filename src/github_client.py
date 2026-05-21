"""GitHub REST + GraphQL helpers used by the processor."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "magmamoose-comment-commander"


class GitHubError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class RepositoryRef:
    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class GitHubInstance:
    """A single GitHub-or-GHE deployment the bot is configured against.

    Each instance carries its own PAT and git author identity. Detection
    of which instance a webhook belongs to happens by matching the
    payload's `repository.html_url` against `host`.
    """
    name: str
    host: str
    api_base: str
    graphql_url: str
    pat: str
    author_name: str
    author_email: str

    @classmethod
    def github_com(cls, *, pat: str, author_name: str, author_email: str) -> "GitHubInstance":
        return cls(
            name="github",
            host="github.com",
            api_base="https://api.github.com",
            graphql_url="https://api.github.com/graphql",
            pat=pat, author_name=author_name, author_email=author_email,
        )

    @classmethod
    def ghe(cls, *, host: str, pat: str, author_name: str, author_email: str) -> "GitHubInstance":
        return cls(
            name="ghe",
            host=host,
            api_base=f"https://{host}/api/v3",
            graphql_url=f"https://{host}/api/graphql",
            pat=pat, author_name=author_name, author_email=author_email,
        )

    def clone_url(self, owner: str, repo: str) -> str:
        return f"https://x-access-token:{self.pat}@{self.host}/{owner}/{repo}.git"

    def repo_url(self, owner: str, repo: str) -> str:
        return f"https://{self.host}/{owner}/{repo}"

    def html_url_prefix(self) -> str:
        return f"https://{self.host}/"


def find_instance_for_payload(
    payload: dict[str, Any], instances: list[GitHubInstance]
) -> GitHubInstance | None:
    """Pick the right instance for an incoming webhook by inspecting URLs."""
    candidates = []
    repo = payload.get("repository")
    if isinstance(repo, dict):
        candidates.append(repo.get("html_url"))
        candidates.append(repo.get("clone_url"))
    pull = payload.get("pull_request")
    if isinstance(pull, dict):
        candidates.append(pull.get("html_url"))
    for url in candidates:
        if not isinstance(url, str):
            continue
        for inst in instances:
            if url.startswith(inst.html_url_prefix()):
                return inst
    return None


def find_instance_for_host(host: str, instances: list[GitHubInstance]) -> GitHubInstance | None:
    host = host.lower().strip()
    for inst in instances:
        if inst.host.lower() == host:
            return inst
    return None


@dataclass(frozen=True)
class ReviewComment:
    id: int
    node_id: str | None
    user_login: str | None
    body: str
    path: str
    diff_hunk: str | None
    line: int | None
    side: str | None
    in_reply_to_id: int | None = None


@dataclass(frozen=True)
class ReviewThread:
    id: str
    is_resolved: bool


def verify_signature(body: bytes, header: str | None, secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    received = header[len("sha256="):]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        rest_base: str = GITHUB_API_BASE,
        graphql_url: str | None = None,
        timeout: float = 30.0,
    ):
        self._graphql_url = graphql_url or f"{rest_base.rstrip('/')}/graphql"
        self._client = httpx.Client(
            base_url=rest_base,
            headers={
                "accept": "application/vnd.github+json",
                "authorization": f"Bearer {token}",
                "user-agent": USER_AGENT,
                "x-github-api-version": GITHUB_API_VERSION,
            },
            timeout=timeout,
        )

    @classmethod
    def for_instance(cls, instance: "GitHubInstance", *, timeout: float = 30.0) -> "GitHubClient":
        return cls(
            instance.pat,
            rest_base=instance.api_base,
            graphql_url=instance.graphql_url,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- REST -----------------------------------------------------------------

    def get_file(self, repo: RepositoryRef, path: str, ref: str) -> dict[str, Any] | None:
        response = self._client.get(
            f"/repos/{quote(repo.owner)}/{quote(repo.repo)}/contents/{_path(path)}",
            params={"ref": ref},
        )
        if response.status_code == 404:
            return None
        if not response.is_success:
            raise GitHubError(f"get_file failed ({response.status_code}): {response.text[:200]}")
        return response.json()

    @staticmethod
    def decode_file_content(payload: dict[str, Any]) -> str | None:
        if payload.get("type") != "file" or payload.get("encoding") != "base64":
            return None
        content = payload.get("content")
        if not isinstance(content, str):
            return None
        try:
            return base64.b64decode(content).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None

    def list_review_comments(
        self, repo: RepositoryRef, pr_number: int, review_id: int
    ) -> list[ReviewComment]:
        response = self._client.get(
            f"/repos/{quote(repo.owner)}/{quote(repo.repo)}/pulls/{pr_number}"
            f"/reviews/{review_id}/comments"
        )
        if not response.is_success:
            raise GitHubError(
                f"list_review_comments failed ({response.status_code}): {response.text[:200]}"
            )
        return [parse_review_comment(entry) for entry in response.json() if isinstance(entry, dict)]

    def list_pr_review_comments(
        self, repo: RepositoryRef, pr_number: int
    ) -> list[ReviewComment]:
        """List ALL review comments on a PR (not scoped to a single review)."""
        out: list[ReviewComment] = []
        page = 1
        while True:
            response = self._client.get(
                f"/repos/{quote(repo.owner)}/{quote(repo.repo)}/pulls/{pr_number}/comments",
                params={"per_page": 100, "page": page},
            )
            if not response.is_success:
                raise GitHubError(
                    f"list_pr_review_comments failed ({response.status_code}): {response.text[:200]}"
                )
            items = response.json()
            if not isinstance(items, list) or not items:
                break
            for entry in items:
                if isinstance(entry, dict):
                    out.append(parse_review_comment(entry))
            if len(items) < 100:
                break
            page += 1
        return out

    def get_pull_request(self, repo: RepositoryRef, pr_number: int) -> dict[str, Any]:
        response = self._client.get(
            f"/repos/{quote(repo.owner)}/{quote(repo.repo)}/pulls/{pr_number}"
        )
        if not response.is_success:
            raise GitHubError(
                f"get_pull_request failed ({response.status_code}): {response.text[:200]}"
            )
        body = response.json()
        if not isinstance(body, dict):
            raise GitHubError("get_pull_request returned non-object payload")
        return body

    def reply_to_comment(
        self, repo: RepositoryRef, pr_number: int, comment_id: int, body: str
    ) -> None:
        response = self._client.post(
            f"/repos/{quote(repo.owner)}/{quote(repo.repo)}/pulls/{pr_number}"
            f"/comments/{comment_id}/replies",
            json={"body": body},
        )
        if not response.is_success:
            raise GitHubError(
                f"reply_to_comment failed ({response.status_code}): {response.text[:200]}"
            )

    # --- GraphQL --------------------------------------------------------------

    def find_review_thread(
        self, repo: RepositoryRef, pr_number: int, comment: ReviewComment
    ) -> ReviewThread | None:
        cursor: str | None = None
        for _ in range(5):
            data = self._graphql(
                """
                query ($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
                  repository(owner: $owner, name: $repo) {
                    pullRequest(number: $number) {
                      reviewThreads(first: 100, after: $cursor) {
                        pageInfo { hasNextPage endCursor }
                        nodes {
                          id
                          isResolved
                          comments(first: 100) {
                            nodes { id databaseId }
                          }
                        }
                      }
                    }
                  }
                }
                """,
                {
                    "owner": repo.owner,
                    "repo": repo.repo,
                    "number": pr_number,
                    "cursor": cursor,
                },
            )
            pull = (
                data.get("data", {})
                .get("repository", {})
                .get("pullRequest", {})
            )
            threads = pull.get("reviewThreads") if isinstance(pull, dict) else None
            if not isinstance(threads, dict):
                return None
            for node in threads.get("nodes") or []:
                if not isinstance(node, dict):
                    continue
                comments = (node.get("comments") or {}).get("nodes") or []
                if _matches_comment(comments, comment):
                    thread_id = node.get("id")
                    resolved = node.get("isResolved")
                    if isinstance(thread_id, str) and isinstance(resolved, bool):
                        return ReviewThread(id=thread_id, is_resolved=resolved)
            page = threads.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return None
            cursor = page.get("endCursor")
        return None

    def resolve_thread(self, thread_id: str) -> None:
        self._graphql(
            """
            mutation ($threadId: ID!) {
              resolveReviewThread(input: { threadId: $threadId }) {
                thread { id isResolved }
              }
            }
            """,
            {"threadId": thread_id},
        )

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        # GHE's GraphQL endpoint is at /api/graphql (not /api/v3/graphql),
        # so we use an absolute URL rather than a path relative to rest_base.
        response = self._client.post(
            self._graphql_url,
            json={"query": query, "variables": variables},
        )
        if not response.is_success:
            raise GitHubError(
                f"graphql failed ({response.status_code}): {response.text[:200]}"
            )
        body = response.json()
        if isinstance(body, dict) and body.get("errors"):
            raise GitHubError(f"graphql errors: {body['errors']}")
        return body if isinstance(body, dict) else {}

    # --- new REST helpers ------------------------------------------------

    def list_pr_commits(self, repo: RepositoryRef, pr_number: int) -> list[dict[str, Any]]:
        """Return all commits in a PR (paginated)."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self._client.get(
                f"/repos/{quote(repo.owner)}/{quote(repo.repo)}/pulls/{pr_number}/commits",
                params={"per_page": 100, "page": page},
            )
            if not response.is_success:
                raise GitHubError(
                    f"list_pr_commits failed ({response.status_code}): {response.text[:200]}"
                )
            items = response.json()
            if not isinstance(items, list) or not items:
                break
            out.extend(item for item in items if isinstance(item, dict))
            if len(items) < 100:
                break
            page += 1
        return out

    def list_repo_hooks(self, repo: RepositoryRef) -> list[dict[str, Any]]:
        return self._list_hooks(f"/repos/{quote(repo.owner)}/{quote(repo.repo)}/hooks")

    def list_org_hooks(self, org: str) -> list[dict[str, Any]]:
        return self._list_hooks(f"/orgs/{quote(org)}/hooks")

    def _list_hooks(self, path: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self._client.get(path, params={"per_page": 100, "page": page})
            if not response.is_success:
                raise GitHubError(
                    f"list_hooks failed ({response.status_code}): {response.text[:300]}",
                    status_code=response.status_code,
                )
            items = response.json()
            if not isinstance(items, list) or not items:
                break
            out.extend(item for item in items if isinstance(item, dict))
            if len(items) < 100:
                break
            page += 1
        return out

    def create_repo_hook(
        self,
        repo: RepositoryRef,
        *,
        webhook_url: str,
        secret: str,
        events: list[str],
    ) -> dict[str, Any]:
        return self._create_hook(
            f"/repos/{quote(repo.owner)}/{quote(repo.repo)}/hooks",
            webhook_url=webhook_url, secret=secret, events=events,
        )

    def create_org_hook(
        self,
        org: str,
        *,
        webhook_url: str,
        secret: str,
        events: list[str],
    ) -> dict[str, Any]:
        return self._create_hook(
            f"/orgs/{quote(org)}/hooks",
            webhook_url=webhook_url, secret=secret, events=events,
        )

    def _create_hook(
        self,
        path: str,
        *,
        webhook_url: str,
        secret: str,
        events: list[str],
    ) -> dict[str, Any]:
        payload = {
            "name": "web",
            "active": True,
            "events": events,
            "config": {
                "url": webhook_url,
                "content_type": "json",
                "secret": secret,
                "insecure_ssl": "0",
            },
        }
        response = self._client.post(path, json=payload)
        if not response.is_success:
            raise GitHubError(
                f"create_hook failed ({response.status_code}): {response.text[:300]}",
                status_code=response.status_code,
            )
        body = response.json()
        if not isinstance(body, dict):
            raise GitHubError("create_hook returned non-object payload")
        return body


def parse_review_comment(payload: dict[str, Any]) -> ReviewComment:
    user = payload.get("user")
    in_reply = payload.get("in_reply_to_id")
    return ReviewComment(
        id=int(payload["id"]),
        node_id=payload.get("node_id") if isinstance(payload.get("node_id"), str) else None,
        user_login=user.get("login") if isinstance(user, dict) else None,
        body=payload.get("body") or "",
        path=payload.get("path") or "",
        diff_hunk=payload.get("diff_hunk") if isinstance(payload.get("diff_hunk"), str) else None,
        line=payload.get("line") if isinstance(payload.get("line"), int) else None,
        side=payload.get("side") if isinstance(payload.get("side"), str) else None,
        in_reply_to_id=in_reply if isinstance(in_reply, int) else None,
    )


def _matches_comment(comment_nodes: Iterable[Any], comment: ReviewComment) -> bool:
    for entry in comment_nodes:
        if not isinstance(entry, dict):
            continue
        if comment.node_id and entry.get("id") == comment.node_id:
            return True
        if entry.get("databaseId") == comment.id:
            return True
    return False


def _path(value: str) -> str:
    return "/".join(quote(segment) for segment in value.split("/"))
