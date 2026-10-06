import pytest

from carcara.profiles import MODEL_KEYS, ProfileError, list_profiles, load_profile, parse_profile
from carcara.resources import iter_claude_templates, render

VALID = "".join(f"{k}=m-{i}\n" for i, k in enumerate(MODEL_KEYS))


def write(tmp_path, text, name="p.env"):
    p = tmp_path / name
    p.write_bytes(text.encode() if isinstance(text, str) else text)
    return str(p)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("economy", {"MODEL_ARCHITECT": "sonnet", "MODEL_TEST_RUNNER": "haiku"}),
        ("balanced", {"MODEL_ARCHITECT": "opus", "MODEL_IMPLEMENTER": "sonnet"}),
        ("quality", {"MODEL_IMPLEMENTER": "opus", "MODEL_TEST_RUNNER": "haiku"}),
    ],
)
def test_packaged_profiles(name, expected):
    prof = load_profile(name)
    assert prof.name == name
    assert set(prof.models) == set(MODEL_KEYS)
    for key, value in expected.items():
        assert prof.models[key] == value


def test_economy_has_no_opus():
    assert "opus" not in load_profile("economy").models.values()


def test_file_profile_name_is_basename(tmp_path):
    prof = load_profile(write(tmp_path, VALID, "my.env"))
    assert prof.name == "my"
    assert prof.models["MODEL_MAIN"] == "m-0"


def test_file_without_env_suffix(tmp_path):
    assert load_profile(write(tmp_path, VALID, "custom")).name == "custom"


def test_file_named_dot_env_keeps_name(tmp_path):
    # basename ".env" .env == ".env" -> valid name per the safe regex
    assert load_profile(write(tmp_path, VALID, ".env")).name == ".env"


def test_invalid_profile_name(tmp_path):
    with pytest.raises(ProfileError, match="invalid profile name: a b"):
        load_profile(write(tmp_path, VALID, "a b.env"))


def test_comments_and_blank_lines_skipped(tmp_path):
    assert load_profile(write(tmp_path, "# c\n\n" + VALID + "\n#x\n")).models["MODEL_MAIN"]


def test_last_line_without_newline(tmp_path):
    prof = load_profile(write(tmp_path, VALID.rstrip("\n")))
    assert prof.models["MODEL_DOC_WRITER"] == "m-6"


def test_later_value_wins(tmp_path):
    assert load_profile(write(tmp_path, VALID + "MODEL_MAIN=x\n")).models["MODEL_MAIN"] == "x"


def test_split_on_first_equals(tmp_path):
    with pytest.raises(ProfileError, match="invalid model for MODEL_MAIN in .*: 'a=b'"):
        load_profile(write(tmp_path, VALID + "MODEL_MAIN=a=b\n"))


def test_line_without_equals_uses_whole_line_as_value():
    # ${line#*=} with no '=' is the whole line, matching bash.
    assert parse_profile((VALID + "MODEL_MAIN\n").encode(), "x")["MODEL_MAIN"] == "MODEL_MAIN"


@pytest.mark.parametrize("line", ["BOGUS=x", " MODEL_MAIN=x", "   ", "model_main=x"])
def test_unknown_key_fatal(tmp_path, line):
    p = write(tmp_path, VALID + line + "\n")
    with pytest.raises(ProfileError, match=f"invalid key in profile {p}: "):
        load_profile(p)


@pytest.mark.parametrize("value", ["so/net", "", "a b", "x;y", "sonnet\r", "é"])
def test_unsafe_value_rejected(tmp_path, value):
    p = write(tmp_path, VALID + f"MODEL_MAIN={value}\n")
    with pytest.raises(ProfileError) as exc:
        load_profile(p)
    assert str(exc.value) == f"invalid model for MODEL_MAIN in {p}: '{value}'"


def test_crlf_profile_rejected(tmp_path):
    with pytest.raises(ProfileError, match="invalid model for MODEL_MAIN"):
        load_profile(write(tmp_path, VALID.replace("\n", "\r\n")))


@pytest.mark.parametrize("missing", MODEL_KEYS)
def test_each_key_required(tmp_path, missing):
    text = "".join(line + "\n" for line in VALID.splitlines() if not line.startswith(missing + "="))
    p = write(tmp_path, text)
    with pytest.raises(ProfileError) as exc:
        load_profile(p)
    assert str(exc.value) == f"profile {p} is missing {missing}"


def test_missing_keys_reported_in_order(tmp_path):
    p = write(tmp_path, "# empty\n")
    with pytest.raises(ProfileError, match="is missing MODEL_MAIN$"):
        load_profile(p)


@pytest.mark.parametrize("spec", ["", "../x", "a/b", "x.env", "nope.y"])
def test_path_like_unknown_profile(spec, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ProfileError) as exc:
        load_profile(spec)
    assert str(exc.value) == f"unknown profile: {spec}"


def test_unknown_bare_name(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ProfileError) as exc:
        load_profile("nope")
    assert str(exc.value) == "unknown profile: nope (try --list-profiles)"


def test_existing_file_beats_packaged_name(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, VALID, "balanced")
    assert load_profile("balanced").models["MODEL_MAIN"] == "m-0"


def test_list_profiles():
    out = list_profiles()
    names = [line for line in out.splitlines() if not line.startswith("  ")]
    assert names == ["balanced", "economy", "quality"]
    assert "  MODEL_ARCHITECT=opus\n" in out
    assert out.count("  MODEL_") == 3 * len(MODEL_KEYS)


def test_templates_sorted_bytewise():
    rels = [rel for rel, _ in iter_claude_templates()]
    assert rels == sorted(rels, key=str.encode)
    assert "settings.json" in rels and "agents/explorer.md" in rels


def test_render_replaces_all_placeholders():
    prof = load_profile("quality")
    text = "{{MODEL_IMPLEMENTER}} {{PROFILE}} {{MODEL_IMPLEMENTER}} {{OTHER}}"
    assert render(text, prof) == "opus quality opus {{OTHER}}"
