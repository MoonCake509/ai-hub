"""Список ролей: хаб и веб-панель должны называть роли одинаково.

Ошибка, которую ловит тест: интерфейс рисовал карточку `architect`, а хаб хранил
назначения под `planner` — назначения роли не отображались и терялись при правке.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hub"))

from app import db  # noqa: E402
APP_JS = ROOT / "web" / "app.js"


def role_meta_keys() -> list[str]:
    text = APP_JS.read_text(encoding="utf-8")
    block = re.search(r"const ROLE_META = \{(.*?)\n\};", text, re.S)
    assert block, "ROLE_META не найден в web/app.js"
    return re.findall(r"^\s*([a-z_]+):\s*\[", block.group(1), re.M)


def test_orchestrator_is_first_role():
    assert db.ROLE_IDS[0] == "orchestrator"
    assert db.ROLE_LABELS["orchestrator"] == "Оркестратор"


def test_ui_and_hub_roles_match():
    assert role_meta_keys() == list(db.ROLE_IDS)


def test_no_legacy_architect_key_in_ui():
    assert "architect" not in role_meta_keys()
    assert "planner" in db.ROLE_IDS
