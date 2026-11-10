"""The Keycloak login theme: the sign-in pages share the app's design system.

The pages themselves are rendered by Keycloak, so these tests guard the pieces
this repository controls: the theme files, the realm and compose wiring, and
that the theme's colours cannot drift from the app's design tokens.
"""

import json
import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
THEME = ROOT / "deploy" / "keycloak" / "themes" / "leyline" / "login"
CSS = THEME / "resources" / "css" / "leyline.css"
APP_CSS = ROOT / "frontend" / "src" / "styles" / "main.css"

# theme variable -> app design token (frontend/src/styles/main.css)
TOKENS = {
    "--l-bg": "--color-bg",
    "--l-bg-deep": "--color-bg-deep",
    "--l-panel": "--color-panel",
    "--l-line": "--color-line",
    "--l-line-strong": "--color-line-strong",
    "--l-text": "--color-text",
    "--l-muted": "--color-muted",
    "--l-lime": "--color-lime",
    "--l-lime-dim": "--color-lime-dim",
    "--l-lime-ink": "--color-lime-ink",
    "--l-info": "--color-info",
    "--l-warn": "--color-warn",
    "--l-danger": "--color-danger",
}


def value(css: str, name: str) -> str:
    match = re.search(rf"{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", css)
    assert match, f"{name} not found"
    return match.group(1).lower()


def test_the_theme_extends_the_stock_v2_theme_and_replaces_only_the_styles():
    props = (THEME / "theme.properties").read_text(encoding="utf-8")
    assert "parent=keycloak.v2" in props
    assert "styles=css/leyline.css" in props


@pytest.mark.parametrize(("theme_var", "app_token"), TOKENS.items())
def test_theme_colours_match_the_app_design_tokens(theme_var, app_token):
    assert value(CSS.read_text(encoding="utf-8"), theme_var) == value(
        APP_CSS.read_text(encoding="utf-8"), app_token
    )


def test_every_asset_the_stylesheet_references_exists():
    css = CSS.read_text(encoding="utf-8")
    urls = re.findall(r'url\("([^"]+)"\)', css)
    assert len(urls) >= 8  # seven fonts and the globe
    for relative in urls:
        assert (CSS.parent / relative).resolve().is_file(), relative


def test_the_stylesheet_loads_nothing_from_the_network():
    css = CSS.read_text(encoding="utf-8")
    assert not re.search(r"https?://|//cdn|@import", css)


def test_the_theme_uses_the_same_fonts_as_the_app():
    css = CSS.read_text(encoding="utf-8")
    for family in ("Silkscreen", "JetBrains Mono", "IBM Plex Sans"):
        assert f'font-family: "{family}"' in css
    fonts = THEME / "resources" / "fonts"
    for licence in ("silkscreen", "jetbrains-mono", "ibm-plex-sans"):
        assert (
            fonts / f"LICENSE-{licence}.txt"
        ).is_file()  # OFL: ship the licence with the fonts


def test_the_globe_is_a_valid_png_of_the_expected_size():
    data = (THEME / "resources" / "img" / "globe.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (112, 112)


def test_the_dithered_landscape_is_a_valid_png_of_the_expected_size():
    data = (THEME / "resources" / "img" / "dither.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (480, 48)  # drawn at 4x in the stylesheet


def test_the_footer_label_sits_on_a_solid_plate_over_the_dithered_landscape():
    css = CSS.read_text(encoding="utf-8")
    block = css.split(".pf-v5-c-login__container::after", 1)[1].split("}", 1)[0]
    assert "background: var(--l-bg-deep)" in block  # readable over the dither


def test_the_realm_uses_the_theme_and_sets_a_language_for_screen_readers():
    realm = json.loads(
        (ROOT / "deploy" / "keycloak" / "leyline-realm.json").read_text(
            encoding="utf-8"
        )
    )
    assert realm["loginTheme"] == "leyline"
    assert (
        realm["displayName"] == "Leyline"
    )  # the wordmark; the stylesheet adds the slash
    # Without internationalisation Keycloak omits <html lang>, an accessibility failure.
    assert realm["internationalizationEnabled"] is True
    assert realm["supportedLocales"] == ["en"] and realm["defaultLocale"] == "en"


def test_compose_mounts_the_theme_into_keycloak():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "./deploy/keycloak/themes/leyline:/opt/keycloak/themes/leyline:ro" in compose


def test_the_page_texts_are_overridden_in_one_place():
    messages = (THEME / "messages" / "messages_en.properties").read_text(
        encoding="utf-8"
    )
    assert "loginAccountTitle=Sign in" in messages and "doLogIn=Sign in" in messages
