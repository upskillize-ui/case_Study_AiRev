"""Shared test isolation.

The link renderer keeps two process-lifetime maps — the page cache (open each
link once per run) and the shell map (a page served for two different urls is
the site's own, not a learner's). Both are correct in production and both leak
between tests, where every fixture reuses the same handful of fake urls.

Clearing them before each test keeps the caches real in production and the
tests honest, without either side pretending the other does not exist.
"""

import pytest


@pytest.fixture(autouse=True)
def _fresh_link_state():
    from app.services import link_renderer
    link_renderer.forget_links()
    link_renderer.forget_pages()
    yield
    link_renderer.forget_links()
    link_renderer.forget_pages()
