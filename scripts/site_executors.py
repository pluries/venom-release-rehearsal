"""Page generator of the documentation site (story #3621, task #3635): one page per executor README under `executors/`."""
from __future__ import annotations

import re
from pathlib import Path

_PREFIX = "Venom - Executor "
_H1 = re.compile(r"^#[ \t]+(.+?)\s*$", re.MULTILINE)
_FIXED_TITLES = {"plugins": "Plugins"}


def _title(readme: Path, directory: str) -> str:
    if directory in _FIXED_TITLES:
        return _FIXED_TITLES[directory]
    match = _H1.search(readme.read_text(encoding="utf-8-sig"))
    heading = match.group(1) if match else directory
    return heading.removeprefix(_PREFIX).strip() or directory


def _page(slug: str, title: str, source: str, group: str, description: str) -> dict:
    return {"slug": slug, "title": title, "source": source, "group": group, "description": description}


def expand(root: Path, entry: dict) -> list[dict]:
    """The page entries for `{"generator": "executors"}`: one per `executors/<dir>/README.md`, sorted by directory name."""
    group = entry.get("group", "Executors")
    pages = []
    for readme in sorted((root / "executors").glob("*/README.md"), key=lambda p: p.parent.name):
        name = readme.parent.name
        title = _title(readme, name)
        description = f"How to use the {title} executor in a venom test suite."
        if name in _FIXED_TITLES:
            description = "How to build and load a venom executor as a Go plugin."
        pages.append(_page(name, title, f"executors/{name}/README.md", group, description))
    if (root / "executors" / "README.md").is_file():
        pages.append(_page("writing-executors", "Writing executors", "executors/README.md", group,
                           "How to write a new venom executor."))
    return pages
