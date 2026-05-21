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
    pass


@dataclass(frozen=True)
class RepositoryRef:
    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


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
    def __init__(self, token: str, *, timeout: float = 30.0):
        self._client = httpx.Client(
            base_url=GITHUB_API_BASE,
            headers={
                "accept": "application/vnd.github+json",
                "authorization": f"Bearer {token}",
                "user-agent": USER_AGENT,
                "x-github-api-version": GITHUB_API_VERSION,
            },
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
        response = self._client.post(
            "/graphql",
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
