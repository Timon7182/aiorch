"""Resolve the git repositories backing a project.

A project's path is usually a git repo itself. But some projects use a
parent folder whose immediate children are the actual repos (e.g. a folder
holding ``backend/`` and ``frontend/``). These helpers detect that layout so
git features can operate on a chosen child repo instead of the empty parent.
"""

from pathlib import Path


def resolve_git_repos(project_path: str) -> list[dict]:
    """Return the git repos for a project.

    If ``project_path`` is itself a git repo, returns a single-element list for
    it. Otherwise scans immediate child directories (following symlinks) and
    returns every child that contains a ``.git`` entry.

    Each entry is ``{"name": str, "path": str, "isRoot": bool}``.
    """
    root = Path(project_path)

    if (root / ".git").exists():
        return [{"name": root.name, "path": str(root), "isRoot": True}]

    repos: list[dict] = []
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return repos

    for child in children:
        try:
            if child.is_dir() and (child / ".git").exists():
                repos.append({"name": child.name, "path": str(child), "isRoot": False})
        except OSError:
            continue
    return repos


def resolve_repo_cwd(project_path: str, repo: str | None) -> str:
    """Pick the git working directory for a project operation.

    ``repo`` is an absolute path chosen by the client. It is honored only if it
    matches one of the repos discovered for ``project_path`` — this prevents a
    client from pointing git operations at an arbitrary directory. Falls back
    to the first discovered repo, then to ``project_path`` itself.
    """
    repos = resolve_git_repos(project_path)
    if repo:
        allowed = {r["path"] for r in repos}
        if repo in allowed:
            return repo
    if repos:
        return repos[0]["path"]
    return project_path
