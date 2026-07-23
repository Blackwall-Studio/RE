from pathlib import Path

import pytest

from synapse.config import load_config

ROOT = Path(__file__).resolve().parent.parent


def test_load_default_config():
    cfg = load_config(ROOT / "config" / "models.yaml")
    assert cfg.gateway.base_url.startswith("https://")
    assert len(cfg.locals) >= 2  # ollama + lmstudio
    assert any(p.name == "ollama" for p in cfg.locals)
    assert len(cfg.roster) >= 5
    assert cfg.brain.prefer_local_for_learning is True
    assert cfg.brain.path.endswith("brain.db")


def test_roster_entries():
    cfg = load_config(ROOT / "config" / "models.yaml")
    ids = [m.id for m in cfg.roster]
    assert any("kimi" in i for i in ids)
    assert any("gemini" in i for i in ids)
    for m in cfg.roster:
        assert m.label
        # Implicit default is "gateway"; an entry routed through a peer
        # gateway (e.g. Freebuff) advertises its gateway name explicitly.
        assert m.provider in ("gateway", "freebuff"), (
            f"unexpected provider '{m.provider}' on roster entry {m.id}"
        )


def test_extra_gateways_loaded(monkeypatch):
    """Hermetic: clears FREEBUFF_API_KEY so the api_key=='' assertion isn't
    sensitive to whatever the developer's local .env happens to contain."""
    monkeypatch.delenv("FREEBUFF_API_KEY", raising=False)
    cfg = load_config(ROOT / "config" / "models.yaml")
    freebuff = next((g for g in cfg.extra_gateways if g.name == "freebuff"), None)
    assert freebuff is not None, "freebuff extra_gateway missing from models.yaml"
    assert freebuff.base_url == "https://freebuff.llm.pm/v1"
    assert freebuff.default_model == "minimax-m3"
    assert freebuff.api_key_env == "FREEBUFF_API_KEY"
    assert freebuff.api_key == ""  # env unset at test time (hermetic)


def test_colibri_local_provider_loaded(monkeypatch):
    """Hermetic: clears COLIBRI_BASE_URL/KEY so the ${COLIBRI_BASE_URL}
    interpolation collapses cleanly even when a developer has these in .env."""
    monkeypatch.delenv("COLIBRI_BASE_URL", raising=False)
    monkeypatch.delenv("COLIBRI_API_KEY", raising=False)
    cfg = load_config(ROOT / "config" / "models.yaml")
    names = [p.name for p in cfg.locals]
    assert "colibri" in names
    # base_url is ${COLIBRI_BASE_URL}; hermetic test -> collapses to "" if env unset.
    # We only assert presence + name here; the env-var expansion is exercised
    # by setting COLIBRI_BASE_URL in a separate test if needed.


def test_roster_freebuff_entry_is_known():
    cfg = load_config(ROOT / "config" / "models.yaml")
    ids = [m.id for m in cfg.roster]
    assert "freebuff/minimax-m3" in ids
    fb = next(m for m in cfg.roster if m.id == "freebuff/minimax-m3")
    assert fb.provider == "freebuff"
    assert "Freebuff" in fb.label, (
        f"label {fb.label!r} should mention the gateway name so users can tell"
        " this roster entry routes via Freebuff, not ZenMux"
    )


def test_env_var_expansion_in_local_provider(monkeypatch):
    """Setting COLIBRI_BASE_URL/_API_KEY must be picked up at load time."""
    monkeypatch.setenv("COLIBRI_BASE_URL", "http://192.168.1.42:8000/v1")
    monkeypatch.setenv("COLIBRI_API_KEY", "colibri-test-key")
    cfg = load_config(ROOT / "config" / "models.yaml")
    colibri = next(p for p in cfg.locals if p.name == "colibri")
    assert colibri.base_url == "http://192.168.1.42:8000/v1"
    assert colibri.api_key == "colibri-test-key"


def test_env_var_expansion_in_extra_gateway(monkeypatch):
    """Setting FREEBUFF_API_KEY must be picked up at load time."""
    monkeypatch.setenv("FREEBUFF_API_KEY", "freebuff-test-key")
    cfg = load_config(ROOT / "config" / "models.yaml")
    freebuff = next(g for g in cfg.extra_gateways if g.name == "freebuff")
    assert freebuff.api_key == "freebuff-test-key"


def test_no_extra_gateway_overlap_in_default_config():
    """Shipped models.yaml lists freebuff + freebuff_local; they're not '/'-prefix
    overlaps (no shared first-segment under a '/' boundary), so load_config accepts."""
    cfg = load_config(ROOT / "config" / "models.yaml")
    names = [g.name for g in cfg.extra_gateways]
    assert names == ["freebuff", "freebuff_local"]


def test_freebuff_local_extra_gateway_loaded(monkeypatch):
    """The local Freebuff gateway proxy entry parses with the expected fields.
    monkeypatch clears the optional API-key env var so the assertion is hermetic
    regardless of whatever the developer has in their .env (the harness .env
    contains FREEBUFF_LOCAL_API_KEY=test-local-shim from prior live-validation
    work; this test must still pass for users without that env var)."""
    monkeypatch.delenv("FREEBUFF_LOCAL_API_KEY", raising=False)
    cfg = load_config(ROOT / "config" / "models.yaml")
    fb_local = next((g for g in cfg.extra_gateways if g.name == "freebuff_local"), None)
    assert fb_local is not None, "freebuff_local extra_gateway missing from models.yaml"
    assert fb_local.base_url == "http://localhost:8080/v1"
    assert fb_local.default_model == "minimax-m3"
    assert fb_local.api_key_env == "FREEBUFF_LOCAL_API_KEY"
    assert fb_local.api_key == ""  # optional env; empty default


def test_extra_gateway_overlap_raises(tmp_path):
    """If two extra_gateways share a '/' sub-name relation, load_config rejects."""
    import textwrap

    yaml_text = textwrap.dedent(
        """
        gateway:
          base_url: https://nope.invalid/v1
          api_key_env: NOPE
        extra_gateways:
          - name: foo
            base_url: https://nope.invalid/v1
            api_key: k
          - name: foo/bar
            base_url: https://nope.invalid/v1
            api_key: k
        """
    ).strip()
    cfg_path = tmp_path / "_overlap.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError, match="overlap"):
        load_config(cfg_path)


def test_extra_gateway_duplicate_name_raises(tmp_path):
    """Two gates with the exact same name is its own (clearer) error."""
    import textwrap

    yaml_text = textwrap.dedent(
        """
        gateway:
          base_url: https://nope.invalid/v1
          api_key_env: NOPE
        extra_gateways:
          - name: foo
            base_url: https://nope.invalid/v1
            api_key: k1
          - name: foo
            base_url: https://nope.invalid/v2
            api_key: k2
        """
    ).strip()
    cfg_path = tmp_path / "_dup.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_config(cfg_path)


def test_extra_gateway_auth_scheme_default_is_bearer(tmp_path):
    """If auth_scheme is omitted, default to 'bearer' for back-compat."""
    import textwrap

    yaml_text = textwrap.dedent(
        """
        gateway:
          base_url: https://nope.invalid/v1
        extra_gateways:
          - name: legacy
            base_url: https://nope.invalid/v1
            api_key: sk-legacy-key
        """
    ).strip()
    cfg_path = tmp_path / "_legacy.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    cfg = load_config(cfg_path)
    g = cfg.extra_gateways[0]
    assert g.auth_scheme == "bearer"
    assert g.auth_param_name == ""


def test_extra_gateway_auth_scheme_x_api_key_parses(tmp_path):
    import textwrap

    yaml_text = textwrap.dedent(
        """
        gateway:
          base_url: https://nope.invalid/v1
        extra_gateways:
          - name: xk
            base_url: https://nope.invalid/v1
            api_key: sk-xk-key
            auth_scheme: x-api-key
            auth_param_name: X-API-Key
        """
    ).strip()
    cfg_path = tmp_path / "_xk.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    cfg = load_config(cfg_path)
    g = next(e for e in cfg.extra_gateways if e.name == "xk")
    assert g.auth_scheme == "x-api-key"
    assert g.auth_param_name == "X-API-Key"


def test_extra_gateway_auth_scheme_normalizes_to_lowercase(tmp_path):
    """Mixed-case YAML authoring should still match the lower-case scheme."""
    import textwrap

    yaml_text = textwrap.dedent(
        """
        gateway:
          base_url: https://nope.invalid/v1
        extra_gateways:
          - name: xk2
            base_url: https://nope.invalid/v1
            api_key: sk-xk2
            auth_scheme: X-API-Key
        """
    ).strip()
    cfg_path = tmp_path / "_xk2.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.extra_gateways[0].auth_scheme == "x-api-key"


def test_extra_gateway_unknown_auth_scheme_raises(tmp_path):
    """Unknown auth_scheme must fail loud — silent fall-through to Bearer is a footgun."""
    import textwrap

    yaml_text = textwrap.dedent(
        """
        gateway:
          base_url: https://nope.invalid/v1
        extra_gateways:
          - name: bogus
            base_url: https://nope.invalid/v1
            api_key: sk-bogus
            auth_scheme: lol-not-a-scheme
        """
    ).strip()
    cfg_path = tmp_path / "_bogus.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError, match="unknown auth_scheme"):
        load_config(cfg_path)


def test_extra_gateway_auth_param_env_default_is_empty(tmp_path):
    """If auth_param_env is omitted, defaults to '' (opt-in)."""
    import textwrap

    yaml_text = textwrap.dedent(
        """
        gateway:
          base_url: https://nope.invalid/v1
        extra_gateways:
          - name: legacy
            base_url: https://nope.invalid/v1
            api_key: sk-legacy
        """
    ).strip()
    cfg_path = tmp_path / "_no_param_env.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    cfg = load_config(cfg_path)
    g = cfg.extra_gateways[0]
    assert g.auth_param_env == ""
    assert g.auth_param_name == ""


def test_extra_gateway_auth_param_env_resolves_from_env(monkeypatch, tmp_path):
    """Setting auth_param_env with an env var set must use the env value."""
    import textwrap
    monkeypatch.setenv("MY_GATEWAY_PARAM", "From_Env_Value")

    yaml_text = textwrap.dedent(
        """
        gateway:
          base_url: https://nope.invalid/v1
        extra_gateways:
          - name: envname
            base_url: https://nope.invalid/v1
            api_key: sk-envname
            auth_param_env: MY_GATEWAY_PARAM
        """
    ).strip()
    cfg_path = tmp_path / "_env.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    cfg = load_config(cfg_path)
    g = cfg.extra_gateways[0]
    assert g.auth_param_env == "MY_GATEWAY_PARAM"
    assert g.auth_param_name == "From_Env_Value"


def test_extra_gateway_auth_param_env_falls_back_to_yaml(monkeypatch, tmp_path):
    """If auth_param_env is set but env var is unset, fall back to yaml auth_param_name."""
    import textwrap
    monkeypatch.delenv("MY_UNSET_PARAM_NAME", raising=False)

    yaml_text = textwrap.dedent(
        """
        gateway:
          base_url: https://nope.invalid/v1
        extra_gateways:
          - name: fb
            base_url: https://nope.invalid/v1
            api_key: sk-fb
            auth_param_env: MY_UNSET_PARAM_NAME
            auth_param_name: yaml_fallback_name
        """
    ).strip()
    cfg_path = tmp_path / "_fallback.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    cfg = load_config(cfg_path)
    g = cfg.extra_gateways[0]
    assert g.auth_param_env == "MY_UNSET_PARAM_NAME"
    assert g.auth_param_name == "yaml_fallback_name"


def test_extra_gateway_basic_split_credentials_via_yaml(tmp_path):
    """YAML: api_key as password, auth_param_name as username, scheme=basic."""
    import textwrap
    yaml_text = textwrap.dedent(
        """
        gateway:
          base_url: https://nope.invalid/v1
        extra_gateways:
          - name: split
            base_url: https://nope.invalid/v1
            api_key: secret-password
            auth_param_name: myuser
            auth_scheme: basic
        """
    ).strip()
    cfg_path = tmp_path / "_split.yaml"
    cfg_path.write_text(yaml_text, encoding="utf-8")
    cfg = load_config(cfg_path)
    g = next(e for e in cfg.extra_gateways if e.name == "split")
    assert g.auth_scheme == "basic"
    assert g.auth_param_name == "myuser"
    assert g.api_key == "secret-password"
