import pytest

from carcara.profiles import load_profile
from carcara.resources import templates_root
from carcara.roles import Role, RoleError, load_roles, model_for, parse_role, to_agent_definition
from carcara.schemas import SCHEMAS, validate

EXPECTED_TOOLS = {
    "architect": ("Read", "Grep", "Glob"),
    "doc-writer": ("Read", "Edit", "Write", "Grep", "Glob"),
    "explorer": ("Read", "Grep", "Glob", "Bash"),
    "implementer": ("Read", "Edit", "Write", "Grep", "Glob", "Bash"),
    "reviewer": ("Read", "Grep", "Glob", "Bash"),
    "test-runner": ("Read", "Grep", "Glob", "Bash"),
}


def _frontmatter_tools(name):
    text = templates_root().joinpath("claude", "agents", f"{name}.md").read_text()
    line = next(ln for ln in text.splitlines() if ln.startswith("tools:"))
    return tuple(t.strip() for t in line.split(":", 1)[1].split(","))


def test_six_roles_parse_with_frontmatter_tools():
    roles = load_roles()
    assert set(roles) == set(EXPECTED_TOOLS)
    for name, role in roles.items():
        assert role.tools == EXPECTED_TOOLS[name] == _frontmatter_tools(name)
        assert role.description
        assert role.prompt.startswith("You are the **")
        assert "---" not in role.prompt.splitlines()[0]


@pytest.mark.parametrize("profile", ["economy", "balanced", "quality"])
def test_model_per_profile(profile):
    prof = load_profile(profile)
    for name, role in load_roles().items():
        key = "MODEL_" + name.upper().replace("-", "_")
        assert model_for(role, prof) == prof.models[key]
        assert model_for(name, prof.models) == prof.models[key]


def test_model_for_known_values():
    assert model_for("test-runner", load_profile("quality")) == "haiku"
    assert model_for("architect", load_profile("economy")) == "sonnet"


def test_parse_role_errors():
    with pytest.raises(RoleError):
        parse_role("no frontmatter")
    with pytest.raises(RoleError):
        parse_role("---\nname: x\n")
    with pytest.raises(RoleError):
        parse_role("---\nname: x\ndescription: d\n---\nbody")


def test_to_agent_definition():
    role = load_roles()["reviewer"]
    ad = to_agent_definition(role, load_profile("balanced"))
    assert ad.tools == list(role.tools)
    assert ad.model == "sonnet"
    assert ad.prompt == role.prompt


def test_schemas_are_strict():
    def walk(schema):
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False
            assert "required" in schema
            for sub in schema["properties"].values():
                walk(sub)
        elif schema.get("type") == "array":
            walk(schema["items"])

    assert set(SCHEMAS) == {"triage", "explore", "plan", "implement", "test", "review", "docs"}
    for schema in SCHEMAS.values():
        walk(schema)


def test_validate():
    assert validate("triage", {"size": "M", "rationale": "x"}) == []
    assert validate("triage", {"size": "XL", "rationale": "x"})
    assert validate("triage", {"size": "S"})
    assert validate("triage", {"size": "S", "rationale": "x", "extra": 1})
    ok_review = {
        "verdict": "request_changes",
        "findings": [{"severity": "major", "path": "a.py", "issue": "i", "fix": "f"}],
    }
    assert validate("review", ok_review) == []
    ok_review["findings"][0]["line"] = True
    assert validate("review", ok_review)
    assert validate("test", {"passed": "yes", "commands": [], "failures": []})
    assert validate("docs", {"changed": ["README.md"]}) == []
    assert validate("docs", {"changed": [1]})


def test_role_model_key():
    assert Role("test-runner", "d", (), "p").model_key == "MODEL_TEST_RUNNER"
