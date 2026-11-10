"""Static checks on the frontend sources and templates.

These repeat what ESLint enforces in `frontend/`, on purpose: the rule that text
from users, documents and the model never becomes markup must hold even if
someone skips the frontend tooling. They also keep the templates, the Vite
entries and the Python package data in step.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_SRC = ROOT / "frontend" / "src"
TEMPLATES = ROOT / "src" / "leyline" / "templates"

SOURCES = sorted(
    p for p in FRONTEND_SRC.rglob("*.ts") if not p.name.endswith(".test.ts")
)
HTML = sorted(TEMPLATES.rglob("*.html"))

UNSAFE_SINKS = [
    r"\.innerHTML",
    r"\.outerHTML",
    r"insertAdjacentHTML",
    r"\.srcdoc",
    r"document\.write",
    r"\beval\(",
    r"new\s+Function\(",
    # model text must never become a link, an image, a frame or a script
    r"""(?:createElement|\bel)\(\s*["'](?:a|img|iframe|script|object|embed|link)["']""",
    r"""\.(?:href|src)\s*=[^=]""",
    r"""setAttribute\(\s*["'](?:href|src|srcdoc|on\w+)["']""",
]


def test_there_are_frontend_sources_to_check():
    assert len(SOURCES) >= 15
    assert len(HTML) >= 6


@pytest.mark.parametrize(
    "path", SOURCES, ids=lambda p: str(p.relative_to(FRONTEND_SRC))
)
def test_frontend_code_never_builds_markup_or_links_from_data(path):
    text = path.read_text(encoding="utf-8")
    for pattern in UNSAFE_SINKS:
        assert not re.search(pattern, text), f"{path.name} matches {pattern}"


@pytest.mark.parametrize("path", HTML, ids=lambda p: str(p.relative_to(TEMPLATES)))
def test_templates_have_no_inline_code_and_never_mark_data_as_safe(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", text), "inline <script>"
    assert "<style" not in text, "inline <style>"
    assert not re.search(r"\sstyle=", text), "inline style attribute"
    assert not re.search(r"\son[a-z]+=", text), "inline event handler"
    assert "|safe" not in text.replace(" ", "") and "autoescape" not in text


def vite_entries() -> list[str]:
    config = (ROOT / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
    match = re.search(r"const entries = \[([^\]]+)\]", config)
    assert match, "entries list not found in vite.config.ts"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_every_vite_entry_has_a_source_file():
    for name in vite_entries():
        assert (FRONTEND_SRC / "entries" / f"{name}.ts").exists(), name


def test_every_template_entry_is_built_by_vite():
    used = {
        m.group(1)
        for path in HTML
        for m in re.finditer(
            r'\{%\s*set entry = "([^"]+)"', path.read_text(encoding="utf-8")
        )
    }
    assert used, "no template declares an entry"
    assert used <= set(vite_entries()), used - set(vite_entries())


def test_every_page_template_declares_its_entry():
    pages = [p for p in HTML if p.name not in {"base.html"} and p.parent == TEMPLATES]
    for path in pages:
        assert re.search(r'\{%\s*set entry = "', path.read_text(encoding="utf-8")), (
            path.name
        )


def test_built_manifest_covers_every_entry_when_the_frontend_is_built():
    from leyline import frontend

    manifest = frontend.load_manifest()
    if manifest is None:
        pytest.skip("frontend not built (npm run build in frontend/)")
    for name in vite_entries():
        assert f"src/entries/{name}.ts" in manifest, name


def test_python_package_data_ships_templates_partials_and_the_build():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for pattern in (
        "templates/*.html",
        "templates/partials/*.html",
        "static/dist/*.json",
        "static/dist/assets/*",
    ):
        assert f'"{pattern}"' in pyproject, pattern
