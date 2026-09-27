"""Raises a single atomic PR into a tnebooks textbook repo with new chapter
content (English + Tamil `_index.md` + images).

Verified against the live tnebooks org this session: 12 repos match the
`<grade>-<subject>` textbook naming convention; the rest are `_questions`
repos or non-textbook repos (excluded). Default branch varies per repo
(`12th-english` uses `main`, the other 11 sampled use `develop`) -- always
read `repo.default_branch` live, never hardcode.

Uses PyGithub's Git Data API (blobs -> tree -> commit -> ref -> PR) to
produce one atomic multi-file commit, rather than the Contents API (one
commit per file, non-atomic) or shelling out to `git` (would need a local
clone for what's fundamentally a small in-memory bundle already sitting in
`Chapter` objects).
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from github import Github, GithubException
from github.InputGitTreeElement import InputGitTreeElement
from github.Repository import Repository

from pdf2md.chapters import Chapter
from pdf2md.hugo_frontmatter import build_chapter_file
from pdf2md.mcq import MCQ

logger = logging.getLogger(__name__)

_TEXTBOOK_RE = re.compile(r"^\d{1,2}(st|nd|rd|th)-[a-z]+$")


@dataclass(frozen=True)
class RepoRef:
    name: str
    full_name: str
    default_branch: str


# Static snapshot of the 12 known textbook repos, captured live this
# session -- used only if the live org listing call fails or rate-limits.
_FALLBACK_REPOS: tuple[RepoRef, ...] = (
    RepoRef("10th-maths", "tnebooks/10th-maths", "develop"),
    RepoRef("10th-science", "tnebooks/10th-science", "develop"),
    RepoRef("10th-social", "tnebooks/10th-social", "develop"),
    RepoRef("11th-botany", "tnebooks/11th-botany", "develop"),
    RepoRef("11th-chemistry", "tnebooks/11th-chemistry", "develop"),
    RepoRef("11th-english", "tnebooks/11th-english", "develop"),
    RepoRef("11th-maths", "tnebooks/11th-maths", "develop"),
    RepoRef("11th-physics", "tnebooks/11th-physics", "develop"),
    RepoRef("12th-chemistry", "tnebooks/12th-chemistry", "develop"),
    RepoRef("12th-english", "tnebooks/12th-english", "main"),
    RepoRef("12th-maths", "tnebooks/12th-maths", "develop"),
    RepoRef("12th-physics", "tnebooks/12th-physics", "develop"),
)


def filter_textbook_repos(repos: Iterable[RepoRef]) -> list[RepoRef]:
    return [r for r in repos if _TEXTBOOK_RE.match(r.name)]


def list_textbook_repos(client: Github, org: str = "tnebooks") -> list[RepoRef]:
    """List tnebooks org repos matching the `<grade>-<subject>` textbook
    naming convention. Falls back to a static snapshot if the live call
    fails (network error, rate limit, etc.) rather than breaking the UI.
    """
    try:
        all_repos = [
            RepoRef(name=r.name, full_name=r.full_name, default_branch=r.default_branch)
            for r in client.get_organization(org).get_repos()
        ]
    except GithubException as exc:
        logger.warning("list_textbook_repos: live call failed, using fallback list: %s", exc)
        return list(_FALLBACK_REPOS)
    return filter_textbook_repos(all_repos)


@dataclass
class PrResult:
    ok: bool
    dry_run: bool
    branch_name: str
    commit_message: str
    tree_entries: list[dict] = field(default_factory=list)
    pr_url: str | None = None
    error: str | None = None


def _chapter_files(chapter: Chapter) -> list[tuple[str, bytes | str]]:
    """(path, content) pairs for one chapter: markdown (str) + images
    (bytes), duplicated byte-identical across content.en and content.ta --
    real tnebooks repos don't follow any consistent image-naming convention
    between the two language trees, so reusing the same filename/bytes in
    both is simplest and always correct."""
    files: list[tuple[str, bytes | str]] = [
        (f"content.en/docs/{chapter.slug}/_index.md", build_chapter_file(chapter, "en")),
        (f"content.ta/docs/{chapter.slug}/_index.md", build_chapter_file(chapter, "ta")),
    ]
    for filename, data in chapter.images:
        files.append((f"content.en/docs/{chapter.slug}/{filename}", data))
        files.append((f"content.ta/docs/{chapter.slug}/{filename}", data))
    return files


def _create_pr_from_files(
    repo: Repository,
    files: list[tuple[str, bytes | str]],
    branch_prefix: str,
    commit_message: str,
    pr_title: str,
    pr_body: str,
    dry_run: bool,
) -> PrResult:
    """Shared Git Data API dance (blobs -> tree -> commit -> ref -> PR) used
    by both `create_chapter_pr` and `create_questions_pr` -- the only
    difference between the two is which files go into the commit."""
    branch_name = f"{branch_prefix}/{repo.name}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"

    tree_entries = [
        {"path": path, "mode": "100644", "type": "blob", "is_binary": isinstance(content, bytes)}
        for path, content in files
    ]

    if dry_run:
        return PrResult(
            ok=True, dry_run=True, branch_name=branch_name,
            commit_message=commit_message, tree_entries=tree_entries,
        )

    try:
        branch = repo.get_branch(repo.default_branch)
        base_commit = repo.get_git_commit(branch.commit.sha)

        tree_elements = []
        for path, content in files:
            if isinstance(content, bytes):
                blob = repo.create_git_blob(base64.b64encode(content).decode("ascii"), "base64")
            else:
                blob = repo.create_git_blob(content, "utf-8")
            tree_elements.append(InputGitTreeElement(path=path, mode="100644", type="blob", sha=blob.sha))

        new_tree = repo.create_git_tree(tree_elements, base_commit.tree)
        new_commit = repo.create_git_commit(commit_message, new_tree, [base_commit])
        repo.create_git_ref(f"refs/heads/{branch_name}", new_commit.sha)
        pull_request = repo.create_pull(
            base=repo.default_branch, head=branch_name, title=pr_title, body=pr_body,
        )
    except GithubException as exc:
        logger.error("_create_pr_from_files failed repo=%s branch=%s: %s", repo.full_name, branch_name, exc)
        return PrResult(
            ok=False, dry_run=False, branch_name=branch_name,
            commit_message=commit_message, tree_entries=tree_entries, error=str(exc),
        )

    logger.info("_create_pr_from_files ok repo=%s branch=%s pr_url=%s", repo.full_name, branch_name, pull_request.html_url)
    return PrResult(
        ok=True, dry_run=False, branch_name=branch_name, commit_message=commit_message,
        tree_entries=tree_entries, pr_url=pull_request.html_url,
    )


def create_chapter_pr(
    repo: Repository,
    chapters: list[Chapter],
    pr_title: str,
    pr_body: str,
    dry_run: bool = True,
) -> PrResult:
    """Create one atomic multi-file commit (every chapter's English + Tamil
    `_index.md` + images) on a new branch off `repo`'s live default branch,
    then open a PR into that same branch.

    `dry_run=True` (default) computes the branch name, commit message, and
    full tree-entry list and returns *before* making any write call --
    same code path used by tests and the wizard's "Dry run" checkbox, so a
    real click-through never touches the live write path unless explicitly
    un-gated.
    """
    commit_message = f"Add {len(chapters)} chapter(s): " + ", ".join(c.title for c in chapters)
    files: list[tuple[str, bytes | str]] = []
    for chapter in chapters:
        files.extend(_chapter_files(chapter))
    return _create_pr_from_files(repo, files, "add-content", commit_message, pr_title, pr_body, dry_run)


def resolve_questions_repo(client: Github, textbook_repo: RepoRef) -> RepoRef | None:
    """Look up the sibling `<repo>_questions` repo in the same org as
    `textbook_repo` (e.g. `tnebooks/12th-physics` -> `tnebooks/12th-physics_questions`).
    Returns `None` -- not an exception -- if it doesn't exist; the caller
    should treat that as "no MCQ repo to publish to" rather than an error,
    since not every subject has one set up yet."""
    questions_full_name = f"{textbook_repo.full_name}_questions"
    try:
        repo = client.get_repo(questions_full_name)
    except GithubException as exc:
        logger.info("resolve_questions_repo: no sibling repo %s (%s)", questions_full_name, exc)
        return None
    return RepoRef(name=repo.name, full_name=repo.full_name, default_branch=repo.default_branch)


def discover_questions_path_convention(repo: Repository) -> str | None:
    """Find the existing `questions/<category>/<subject>/` path prefix a
    `_questions` repo already uses, by walking down from `questions/` while
    each level has exactly one subdirectory and no files -- matches the
    real layout observed in `tnebooks/12th-physics_questions`
    (`questions/science/physics/<chapter-slug>/*.md`). Returns `None` if the
    repo has no `questions/` tree yet (e.g. a newly created, still-empty
    `_questions` repo) or the tree doesn't follow this single-chain shape,
    so the caller can fall back to a default convention instead."""
    try:
        contents = repo.get_contents("questions")
    except GithubException:
        return None

    path = "questions"
    while True:
        if not isinstance(contents, list):
            break
        dirs = [c for c in contents if c.type == "dir"]
        files = [c for c in contents if c.type == "file"]
        if files or len(dirs) != 1:
            break
        path = dirs[0].path
        try:
            contents = repo.get_contents(path)
        except GithubException:
            break
    return path if path != "questions" else None


def resolve_mcq_path_prefix(discovered_prefix: str | None, textbook_repo_name: str) -> tuple[str, bool]:
    """Returns `(path_prefix, used_fallback)`. Prefers the sibling repo's
    own discovered convention; falls back to `questions/<subject-slug>`
    (derived from the textbook repo's `<grade>-<subject>` name) when the
    sibling repo has no established convention to match."""
    if discovered_prefix:
        return discovered_prefix, False
    _, _, subject = textbook_repo_name.partition("-")
    return f"questions/{subject or textbook_repo_name}", True


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_mcq_file(mcq: MCQ, language: str) -> str:
    """language in {"en", "ta"}. Front matter/body format verified against
    a real file in `tnebooks/12th-physics_questions`
    (`questions/science/physics/electrostatics/electric-dipole.md`): YAML
    front matter with `choices` (incorrect options), `answers` (correct
    option(s)), optional `complexity`/`tags`, then a bold question line and
    a fenced ```markdown block with the explanation."""
    if language == "en":
        question, choices, answers, explanation = mcq.question, mcq.choices, mcq.answers, mcq.explanation
    elif language == "ta":
        question = mcq.ta_question or mcq.question
        choices = mcq.ta_choices or mcq.choices
        answers = mcq.ta_answers or mcq.answers
        explanation = mcq.ta_explanation or mcq.explanation
    else:
        raise ValueError(f"language must be 'en' or 'ta', got {language!r}")

    lines = ["---"]
    if mcq.complexity:
        lines.append(f"complexity: {_quote(mcq.complexity)}")
    lines.append("choices:")
    lines += [f"  - {_quote(choice)}" for choice in choices]
    lines.append("answers:")
    lines += [f"  - {_quote(answer)}" for answer in answers]
    if mcq.tags:
        lines.append("tags:")
        lines += [f"  - {_quote(tag)}" for tag in mcq.tags]
    lines.append("---")

    front_matter = "\n".join(lines) + "\n"
    body = f"\n**{question}**\n\n```markdown\n{explanation}\n```\n"
    return front_matter + body


def build_mcq_files(chapter: Chapter, path_prefix: str) -> list[tuple[str, bytes | str]]:
    """(path, content) pairs for every MCQ on `chapter`, one file per
    question (+ a `_ta.md` sibling when it has a Tamil translation, matching
    the existing repo's convention)."""
    files: list[tuple[str, bytes | str]] = []
    for mcq in chapter.mcqs:
        base = f"{path_prefix}/{chapter.slug}/{mcq.id}"
        files.append((f"{base}.md", build_mcq_file(mcq, "en")))
        if mcq.ta_question:
            files.append((f"{base}_ta.md", build_mcq_file(mcq, "ta")))
    return files


def create_questions_pr(
    repo: Repository,
    chapters: list[Chapter],
    path_prefix: str,
    pr_title: str,
    pr_body: str,
    dry_run: bool = True,
) -> PrResult:
    """Same atomic-commit pattern as `create_chapter_pr`, for MCQ files
    against a sibling `_questions` repo instead of the textbook repo."""
    commit_message = f"Add questions for {len(chapters)} chapter(s): " + ", ".join(c.title for c in chapters)
    files: list[tuple[str, bytes | str]] = []
    for chapter in chapters:
        files.extend(build_mcq_files(chapter, path_prefix))
    return _create_pr_from_files(repo, files, "add-questions", commit_message, pr_title, pr_body, dry_run)
