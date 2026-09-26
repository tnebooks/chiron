import json
from pathlib import Path
from unittest.mock import MagicMock

from github import GithubException

from pdf2md.chapters import Chapter
from pdf2md.github_pr import (
    _FALLBACK_REPOS,
    RepoRef,
    create_chapter_pr,
    filter_textbook_repos,
    list_textbook_repos,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_real_repo_refs() -> list[RepoRef]:
    """The real 18-repo tnebooks/orgs/repos API response, captured live
    this session -- ground truth for the textbook-repo filter regex."""
    data = json.loads((FIXTURES / "tnebooks_repos.json").read_text())
    return [RepoRef(name=r["name"], full_name=r["full_name"], default_branch=r["default_branch"]) for r in data]


def test_filter_textbook_repos_matches_real_org_listing():
    repos = _load_real_repo_refs()
    result = filter_textbook_repos(repos)

    names = {r.name for r in result}
    assert names == {
        "10th-maths", "10th-science", "10th-social",
        "11th-botany", "11th-chemistry", "11th-english", "11th-maths", "11th-physics",
        "12th-chemistry", "12th-english", "12th-maths", "12th-physics",
    }
    # excluded: _questions repos and non-textbook repos
    assert "12th-physics_questions" not in names
    assert "10th-science_questions" not in names
    assert "tnebooks.github.io" not in names
    assert "ubuntu-blueprint" not in names

    # default branch is preserved, including the irregular one
    by_name = {r.name: r.default_branch for r in result}
    assert by_name["12th-english"] == "main"
    assert by_name["12th-maths"] == "develop"


def test_list_textbook_repos_uses_client():
    repos = _load_real_repo_refs()
    mock_repos = [MagicMock(name=r.name, full_name=r.full_name, default_branch=r.default_branch) for r in repos]
    for mock_repo, r in zip(mock_repos, repos):
        mock_repo.name = r.name  # MagicMock's `name` kwarg doesn't set .name -- set explicitly

    client = MagicMock()
    client.get_organization.return_value.get_repos.return_value = mock_repos

    result = list_textbook_repos(client, org="tnebooks")

    client.get_organization.assert_called_once_with("tnebooks")
    assert {r.name for r in result} == {r.name for r in filter_textbook_repos(repos)}


def test_list_textbook_repos_falls_back_on_api_error():
    client = MagicMock()
    client.get_organization.side_effect = GithubException(status=403, data={}, message="rate limited")

    result = list_textbook_repos(client)

    assert result == list(_FALLBACK_REPOS)


def _make_chapter(title: str, slug: str, weight: int, with_image: bool = False) -> Chapter:
    images = [(f"{slug}-fig.png", b"fake-bytes")] if with_image else []
    return Chapter(
        id=slug, title=title, slug=slug, weight=weight, summary="Summary",
        en_markdown=f"# Chapter {weight}: {title}\n\nEnglish body.\n",
        ta_markdown="தமிழ் body.\n",
        images=images,
    )


def test_create_chapter_pr_dry_run_makes_zero_write_calls():
    repo = MagicMock()
    repo.name = "12th-maths"
    chapters = [_make_chapter("Complex Numbers", "complex-numbers", 13, with_image=True)]

    result = create_chapter_pr(repo, chapters, pr_title="Add Complex Numbers", pr_body="body", dry_run=True)

    assert result.ok is True
    assert result.dry_run is True
    assert result.branch_name.startswith("add-content/12th-maths-")
    # 2 markdown files + 1 image x 2 language trees = 4 entries
    assert len(result.tree_entries) == 4
    assert sum(1 for e in result.tree_entries if e["is_binary"]) == 2

    repo.get_branch.assert_not_called()
    repo.create_git_blob.assert_not_called()
    repo.create_git_tree.assert_not_called()
    repo.create_git_commit.assert_not_called()
    repo.create_git_ref.assert_not_called()
    repo.create_pull.assert_not_called()


def test_create_chapter_pr_real_flow_wires_git_data_api_correctly():
    repo = MagicMock()
    repo.name = "12th-maths"
    repo.full_name = "tnebooks/12th-maths"
    repo.default_branch = "develop"

    base_commit = MagicMock()
    base_commit.tree = MagicMock(name="base-tree")
    repo.get_branch.return_value.commit.sha = "base-sha-123"
    repo.get_git_commit.return_value = base_commit

    repo.create_git_blob.side_effect = [MagicMock(sha=f"blob-sha-{i}") for i in range(10)]
    new_tree = MagicMock()
    repo.create_git_tree.return_value = new_tree
    new_commit = MagicMock(sha="new-commit-sha")
    repo.create_git_commit.return_value = new_commit
    repo.create_pull.return_value.html_url = "https://github.com/tnebooks/12th-maths/pull/1"

    chapters = [_make_chapter("Complex Numbers", "complex-numbers", 13, with_image=True)]
    result = create_chapter_pr(repo, chapters, pr_title="Add Complex Numbers", pr_body="body", dry_run=False)

    assert result.ok is True
    assert result.pr_url == "https://github.com/tnebooks/12th-maths/pull/1"

    repo.get_branch.assert_called_once_with("develop")
    repo.get_git_commit.assert_called_once_with("base-sha-123")
    assert repo.create_git_blob.call_count == 4  # 2 markdown + 2 image copies (en + ta)

    repo.create_git_tree.assert_called_once()
    tree_call_args = repo.create_git_tree.call_args
    assert tree_call_args[0][1] is base_commit.tree  # base_tree passed as object, not sha

    repo.create_git_commit.assert_called_once_with(result.commit_message, new_tree, [base_commit])
    repo.create_git_ref.assert_called_once_with(f"refs/heads/{result.branch_name}", "new-commit-sha")
    repo.create_pull.assert_called_once_with(
        base="develop", head=result.branch_name, title="Add Complex Numbers", body="body",
    )


def test_create_chapter_pr_error_surfaces_as_clean_result():
    repo = MagicMock()
    repo.name = "12th-maths"
    repo.full_name = "tnebooks/12th-maths"
    repo.default_branch = "develop"
    repo.get_branch.side_effect = GithubException(status=422, data={}, message="branch exists")

    chapters = [_make_chapter("Complex Numbers", "complex-numbers", 13)]
    result = create_chapter_pr(repo, chapters, pr_title="t", pr_body="b", dry_run=False)

    assert result.ok is False
    assert result.error
    assert result.pr_url is None
