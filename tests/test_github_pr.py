import json
from pathlib import Path
from unittest.mock import MagicMock

from github import GithubException

from pdf2md.chapters import Chapter
from pdf2md.github_pr import (
    _FALLBACK_REPOS,
    RepoRef,
    build_mcq_file,
    build_mcq_files,
    create_chapter_pr,
    create_questions_pr,
    discover_questions_path_convention,
    filter_textbook_repos,
    list_textbook_repos,
    resolve_mcq_path_prefix,
    resolve_questions_repo,
)
from pdf2md.mcq import MCQ

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


def _make_mcq(question: str = "Q?") -> MCQ:
    return MCQ(id="q-abc123", question=question, choices=["a", "b"], answers=["c"], explanation="e", complexity="M", tags=["2023"])


def test_resolve_questions_repo_found():
    client = MagicMock()
    repo = MagicMock(name="12th-physics_questions", full_name="tnebooks/12th-physics_questions", default_branch="main")
    repo.name = "12th-physics_questions"
    client.get_repo.return_value = repo

    result = resolve_questions_repo(client, RepoRef("12th-physics", "tnebooks/12th-physics", "develop"))

    client.get_repo.assert_called_once_with("tnebooks/12th-physics_questions")
    assert result == RepoRef("12th-physics_questions", "tnebooks/12th-physics_questions", "main")


def test_resolve_questions_repo_missing_returns_none():
    client = MagicMock()
    client.get_repo.side_effect = GithubException(status=404, data={}, message="not found")

    result = resolve_questions_repo(client, RepoRef("10th-social", "tnebooks/10th-social", "develop"))

    assert result is None


def test_discover_questions_path_convention_walks_single_dir_chain():
    repo = MagicMock()

    def _get_contents(path):
        science = MagicMock(type="dir", path="questions/science")
        physics = MagicMock(type="dir", path="questions/science/physics")
        chapter_dir = MagicMock(type="dir", path="questions/science/physics/electrostatics")
        by_path = {
            "questions": [science],
            "questions/science": [physics],
            "questions/science/physics": [chapter_dir, MagicMock(type="dir", path="other-chapter")],
        }
        return by_path[path]

    repo.get_contents.side_effect = _get_contents

    assert discover_questions_path_convention(repo) == "questions/science/physics"


def test_discover_questions_path_convention_missing_tree_returns_none():
    repo = MagicMock()
    repo.get_contents.side_effect = GithubException(status=404, data={}, message="not found")

    assert discover_questions_path_convention(repo) is None


def test_resolve_mcq_path_prefix_prefers_discovered():
    assert resolve_mcq_path_prefix("questions/science/physics", "12th-physics") == ("questions/science/physics", False)


def test_resolve_mcq_path_prefix_falls_back_to_subject_slug():
    assert resolve_mcq_path_prefix(None, "10th-social") == ("questions/social", True)


def test_build_mcq_file_matches_real_repo_schema():
    content = build_mcq_file(_make_mcq(), "en")

    assert content.startswith("---\n")
    assert 'complexity: "M"\n' in content
    assert 'choices:\n  - "a"\n  - "b"\n' in content
    assert 'answers:\n  - "c"\n' in content
    assert 'tags:\n  - "2023"\n' in content
    assert "**Q?**" in content
    assert "```markdown\ne\n```" in content


def test_build_mcq_files_includes_tamil_sibling_only_when_translated():
    chapter = _make_chapter("Electrostatics", "electrostatics", 1)
    chapter.mcqs = [_make_mcq()]

    files = build_mcq_files(chapter, "questions/science/physics")
    assert [path for path, _ in files] == ["questions/science/physics/electrostatics/q-abc123.md"]

    chapter.mcqs[0].ta_question = "கே?"
    files = build_mcq_files(chapter, "questions/science/physics")
    assert [path for path, _ in files] == [
        "questions/science/physics/electrostatics/q-abc123.md",
        "questions/science/physics/electrostatics/q-abc123_ta.md",
    ]


def test_create_questions_pr_dry_run_makes_zero_write_calls():
    repo = MagicMock()
    repo.name = "12th-physics_questions"
    chapter = _make_chapter("Electrostatics", "electrostatics", 1)
    chapter.mcqs = [_make_mcq()]

    result = create_questions_pr(repo, [chapter], "questions/science/physics", pr_title="Add MCQs", pr_body="body", dry_run=True)

    assert result.ok is True
    assert result.branch_name.startswith("add-questions/12th-physics_questions-")
    assert len(result.tree_entries) == 1
    repo.create_pull.assert_not_called()
