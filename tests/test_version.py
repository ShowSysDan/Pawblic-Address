import os
import re

from version import VERSION

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION)


def test_changelog_leads_with_this_version():
    # CLAUDE.md: every change bumps version.py and adds a CHANGELOG entry to match.
    with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
        first = re.search(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$", f.read(), re.M)
    assert first, "CHANGELOG.md has no '## [x.y.z] - YYYY-MM-DD' entry"
    assert first.group(1) == VERSION, f"CHANGELOG.md starts at {first.group(1)}, version.py says {VERSION}"
