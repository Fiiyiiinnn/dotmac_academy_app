"""Composition-root smoke: the spec declares the full Academy product.

The assembly mirrors the legacy D8 declaration (feature + owner
manifests, API/web routers, one web facet, three roles) on the released
kernel + Learning 0.1.0a6 + dotmac-ui contracts.
"""

from __future__ import annotations

import dotmac_learning
import dotmac_ui

from app.assembly import OWNER_DECLARATIONS, PRODUCT_ID, academy_feature, assembly


def test_assembly_declares_academy_product() -> None:
    assert PRODUCT_ID == "dotmac-academy"
    assert assembly.name == PRODUCT_ID
    assert tuple(assembly.modules) == OWNER_DECLARATIONS
    assert dotmac_learning.module in tuple(assembly.modules)
    assert assembly.ui_contract_version == dotmac_ui.UI_CONTRACT_VERSION
    assert tuple(assembly.packaged_static_dirs) == (dotmac_ui.static_dir(),)
    assert {r.code for r in assembly.role_definitions} == {"student", "instructor", "admin"}
    assert len(assembly.web_facets) == 1
    assert len(academy_feature.routers) == 6
    assert len(academy_feature.web_routers) >= 25


def test_learning_lineage_head_is_lr_0012() -> None:
    names = {p.name for p in dotmac_learning.versions_dir().glob("*.py")}
    assert "lr_0012_completion_capability.py" in names
