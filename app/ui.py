"""Academy's composition boundary for the shared Dotmac UI contract.

``dotmac-ui`` owns the stable asset paths, token roles and theme attribute.
Academy owns its product palette and page composition. Keeping those concerns
here prevents routes, templates and the static mount from each inventing a
slightly different integration.
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

from pathlib import Path
from typing import Final

from dotmac_ui.assets import ASSET_NAMESPACE, static_dir, stylesheet_url
from dotmac_ui.contract import THEME_ATTRIBUTE, UI_CONTRACT_VERSION
from dotmac_ui.theme import DEFAULT_THEME

UI_ASSET_MOUNT: Final[str] = f"/static/{ASSET_NAMESPACE}"
UI_ASSET_DIRECTORY: Final[Path] = static_dir() / ASSET_NAMESPACE
UI_STYLESHEET_URL: Final[str] = stylesheet_url()

# Academy remains light-only until its complete public and authenticated
# surfaces have a dark visual contract. Pinning the shared attribute is more
# honest than running the OS-preference bootstrap against a partial dark theme.
UI_THEME_ATTRIBUTE: Final[str] = THEME_ATTRIBUTE
UI_THEME: Final[str] = DEFAULT_THEME


def template_globals() -> dict[str, str | int]:
    """Values every full-page template needs to consume the UI contract."""
    return {
        "dotmac_ui_contract_version": UI_CONTRACT_VERSION,
        "dotmac_ui_stylesheet_url": UI_STYLESHEET_URL,
        "dotmac_ui_theme_attribute": UI_THEME_ATTRIBUTE,
        "dotmac_ui_theme": UI_THEME,
    }


__all__ = [
    "UI_ASSET_DIRECTORY",
    "UI_ASSET_MOUNT",
    "UI_STYLESHEET_URL",
    "UI_THEME",
    "UI_THEME_ATTRIBUTE",
    "template_globals",
]
