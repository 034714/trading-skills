"""Static invocation contracts, not fresh-session behavioral evaluations."""
from pathlib import Path
import json

import pytest

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("pm-weather", "pm-liquid", "trade", "stock")


@pytest.mark.parametrize("name", NAMES)
def test_explicit_invocation_description(name):
    text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = text.split("---", 2)[1]
    assert f"name: {name}" in frontmatter
    description = next(line for line in frontmatter.splitlines() if line.startswith("description:"))
    assert "明确点名" in description
    assert "未点名" in description
    assert "不" in description
    assert "Use when the user asks" not in description
    assert "disable-model-invocation" not in frontmatter
    assert "user-invocable" not in frontmatter
    assert "## 调用门槛" in text
    assert "编辑、审查本技能文件不等于授权运行市场研究" in text
    assert "不自动加载、读取或运行其他技能" in text


@pytest.mark.parametrize("name", ("pm-weather", "trade", "stock"))
@pytest.mark.parametrize("locale", ("en-US", "zh-CN"))
def test_localized_explicit_invocation(name, locale):
    path = ROOT / "skills" / name / "locales" / f"{locale}.json"
    brief = json.loads(path.read_text(encoding="utf-8"))["brief"]
    expected = "Explicit selection or naming required" if locale == "en-US" else "仅明确选择或点名时调用"
    assert brief.startswith(expected)


def test_readme_explains_scope_and_limitations():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "四个技能都需明确点名" in text
    assert "不是客户端强制" in text
    assert "不自动切换" in text
    assert "20 份仅" in text
    assert "不等于 fresh-session" in text
