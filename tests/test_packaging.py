"""requirements.txt and Dockerfile are code too — and they broke the Space.

22 Aug: the playwright line was appended without a newline, producing
`pillow-heif==0.18.0playwright==1.49.1`. pip refused the whole file, the
Space went to Build error, and nothing in 532 passing tests noticed, because
nothing in 532 passing tests had ever read this file. Now something does.

The duplicate-huggingface_hub rule is in the project instructions for the
same reason: a build-time file with no test is a build-time file that fails
in production instead of in CI.
"""
import os
import re

from packaging.requirements import Requirement

ROOT = os.path.join(os.path.dirname(__file__), "..")
REQ = os.path.join(ROOT, "requirements.txt")
DOCKERFILE = os.path.join(ROOT, "Dockerfile")


def _requirement_lines() -> list:
    with open(REQ, encoding="utf-8") as f:
        raw = f.read()
    lines = []
    for n, line in enumerate(raw.splitlines(), 1):
        stripped = line.split("#")[0].strip()
        if stripped:
            lines.append((n, stripped))
    return lines


def test_every_requirement_line_parses():
    """The exact failure: a line pip cannot read kills the whole build."""
    for n, line in _requirement_lines():
        try:
            Requirement(line)
        except Exception as e:                       # noqa: BLE001 - report it
            raise AssertionError(
                f"requirements.txt line {n} is not a valid requirement: "
                f"{line!r} ({e})")


def test_no_package_is_pinned_twice():
    """Duplicate huggingface_hub lines have failed this Space's build before."""
    seen: dict = {}
    for n, line in _requirement_lines():
        name = Requirement(line).name.lower().replace("_", "-")
        assert name not in seen, (
            f"{name} is pinned twice (lines {seen[name]} and {n}) — "
            f"duplicates fail the Space build")
        seen[name] = n


def test_the_pins_the_agent_cannot_run_without_are_present():
    names = {Requirement(l).name.lower().replace("_", "-")
             for _, l in _requirement_lines()}
    # PyMySQL + cryptography: the eaprep tenant DB will not authenticate
    # without both. playwright: the link renderer imports it.
    for required in ("pymysql", "cryptography", "playwright", "anthropic"):
        assert required in names, f"{required} missing from requirements.txt"
    assert "mysql-connector-python" not in names, "PyMySQL only — never mysql-connector"


def test_the_file_ends_with_a_newline():
    """Because the next person to append a line will not check either."""
    with open(REQ, encoding="utf-8") as f:
        assert f.read().endswith("\n"), "requirements.txt must end with a newline"


def test_the_base_image_is_pinned_to_a_distro_playwright_knows():
    """The second build failure: python:3.11-slim floated to Debian 13, which
    Playwright 1.49 has no package list for. It fell back to Ubuntu 20.04
    names (ttf-ubuntu-font-family, ttf-unifont) that Debian 13 does not
    carry, and the browser install died. Playwright 1.49 supports debian11,
    debian12 and ubuntu20.04/22.04/24.04 — the tag must name one of them."""
    with open(DOCKERFILE, encoding="utf-8") as f:
        docker = f.read()
    base = re.search(r"^FROM\s+(\S+)", docker, re.M)
    assert base, "no FROM line"
    known = ("bookworm", "bullseye", "jammy", "noble", "focal")
    assert any(k in base.group(1) for k in known), (
        f"{base.group(1)} does not name a distro Playwright 1.49 knows "
        f"({', '.join(known)}) — an unpinned tag will float again")


def test_the_browser_is_installed_where_any_uid_can_read_it():
    """A browser in /root/.cache is invisible to a container running as a
    non-root uid — a runtime failure a build cannot show you."""
    with open(DOCKERFILE, encoding="utf-8") as f:
        docker = f.read()
    assert "playwright install" in docker, "Chromium is never installed"
    assert re.search(r"ENV\s+PLAYWRIGHT_BROWSERS_PATH=/opt/", docker), \
        "pin PLAYWRIGHT_BROWSERS_PATH outside a user's home"
    assert "chmod -R a+rX /opt/ms-playwright" in docker, \
        "the browser directory must be world-readable"
