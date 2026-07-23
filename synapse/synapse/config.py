"""Configuration loading for Synapse.

Reads config/models.yaml and resolves secrets from environment (.env).

Supports:
  - One primary gateway (ZenMux by default) for cloud models.
  - Local providers (Ollama, LM Studio, custom) for zero-token inference.
  - Extra gateways (peer-level OpenAI-compatible endpoints) addressed via
    ``<gateway>/<model>`` prefix; an optional ``default_model`` lets a
    bare ``<gateway>`` route to a known model.

Secrets come from ``api_key_env:`` (preferred) or ``${ENV_VAR}`` interpolation
in YAML values (parsed via ``os.path.expandvars``); both work uniformly in
gateway, extra_gateways and local_providers blocks.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class GatewayConfig:
    base_url: str = "https://zenmux.ai/api/v1"
    api_key: str = ""
    timeout: float = 180.0


@dataclass
class ExtraGateway:
    """Peer-level OpenAI-compatible gateway addressed via ``<name>/<model>``.

    Example: ``name="freebuff"``, ``default_model="minimax-m3"``
        ``"freebuff/minimax-m3"`` -> Freebuff client, real model is ``minimax-m3``
        ``"freebuff"``            -> Freebuff client, real model is ``minimax-m3``
    """

    name: str
    base_url: str
    api_key: str = ""
    api_key_env: str = ""
    timeout: float = 180.0
    default_model: str = ""
    # auth_scheme is one of: bearer (default), x-api-key, basic, cookie, header.
    # auth_param_name names the cookie (for `cookie`) or the header (for `header`).
    # For `basic`, api_key may be either 'user:pass' or 'pass' (we append ':' then base64).
    auth_scheme: str = "bearer"
    auth_param_name: str = ""
    # auth_param_env: optional env var name used to resolve auth_param_name
    # at load_config time. Mirror of api_key_env semantics: env value (even
    # empty) wins over yaml fallback; if the env var is unset, fall back to
    # the yaml `auth_param_name`. Lets basic-auth username, cookie name, or
    # custom-header name come from a separate env slot.
    auth_param_env: str = ""


@dataclass
class LocalProvider:
    name: str
    base_url: str
    api_key: str = "local"


@dataclass
class ModelEntry:
    id: str
    label: str
    role: str = "generalist"
    provider: str = "gateway"  # "gateway" or a local provider name
    local: bool = False


@dataclass
class BrainConfig:
    path: str = str(ROOT / "data" / "brain.db")
    prefer_local_for_learning: bool = True
    allow_cloud_learning: bool = False


@dataclass
class AppConfig:
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    locals: list[LocalProvider] = field(default_factory=list)
    extra_gateways: list[ExtraGateway] = field(default_factory=list)
    roster: list[ModelEntry] = field(default_factory=list)
    brain: BrainConfig = field(default_factory=BrainConfig)


def _resolve_secret(env_name: str, yaml_value: str) -> str:
    """env_name-with-fallback pattern, shared by gateway and extra_gateways.

    If ``env_name`` is set:
      - prefer ``os.environ[env_name]`` (even if empty);
      - fall back to ``yaml_value`` only when the env var is *unset*.
    If ``env_name`` is not set or falsy, just use ``yaml_value``.
    """
    if env_name:
        return os.environ.get(env_name, yaml_value)
    return yaml_value


# Valid auth_scheme values for ExtraGateway. Lower-cased; yaml is normalized
# at load time. Reject anything else so misconfigurations fail loud instead of
# silently emitting `Authorization: Bearer` under an unexpected tag.
VALID_AUTH_SCHEMES = frozenset({"bearer", "x-api-key", "basic", "cookie", "header"})


def _validate_auth_schemes(extra_gateways: list[ExtraGateway]) -> None:
    """Reject unknown auth_scheme values with a clear, actionable error."""
    valid = sorted(VALID_AUTH_SCHEMES)
    for g in extra_gateways:
        if g.auth_scheme not in VALID_AUTH_SCHEMES:
            raise ValueError(
                f"extra_gateway {g.name!r} has unknown auth_scheme: "
                f"{g.auth_scheme!r}. Valid values: {', '.join(valid)}"
            )


def _validate_extra_gateway_names(extra_gateways: list[ExtraGateway]) -> None:
    """Reject names that would race in resolve_model.

    Two gates share prefix-overlap iff one name is a ``/``-bounded prefix of
    the other. E.g. ``foo`` + ``foo/bar`` — a request for ``foo/bar/x``
    matches BOTH gates (first-configured-wins). ``foo`` + ``freebuff`` would
    NOT overlap because neither name is ``/``-bounded under the other.
    """
    for i, g1 in enumerate(extra_gateways):
        for g2 in extra_gateways[i + 1 :]:
            if g1.name == g2.name:
                raise ValueError(
                    f"duplicate extra_gateway name: {g1.name!r}"
                )
            if g1.name.startswith(g2.name + "/"):
                raise ValueError(
                    f"extra_gateway names overlap: {g1.name!r} is a sub-name of "
                    f"{g2.name!r}. Rename one so neither name is '/' a prefix of "
                    f"the other, otherwise <name>/<id> requests would race in "
                    f"resolve_model (first-configured-wins)."
                )
            if g2.name.startswith(g1.name + "/"):
                raise ValueError(
                    f"extra_gateway names overlap: {g2.name!r} is a sub-name of "
                    f"{g1.name!r}. Rename one so neither name is '/' a prefix of "
                    f"the other, otherwise <name>/<id> requests would race in "
                    f"resolve_model (first-configured-wins)."
                )


def load_config(path: str | Path | None = None) -> AppConfig:
    load_dotenv(ROOT / ".env")
    cfg_path = Path(path) if path else ROOT / "config" / "models.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    gw = data.get("gateway", {}) or {}
    gateway = GatewayConfig(
        base_url=os.path.expandvars(gw.get("base_url", "https://zenmux.ai/api/v1")),
        api_key=os.path.expandvars(
            _resolve_secret(gw.get("api_key_env", "ZENMUX_API_KEY"), gw.get("api_key", ""))
        ),
        timeout=float(gw.get("timeout", 180)),
    )

    locals_ = [
        LocalProvider(
            name=p["name"],
            base_url=os.path.expandvars(p["base_url"]),
            api_key=os.path.expandvars(p.get("api_key", "local")),
        )
        for p in (data.get("local_providers") or [])
    ]

    extra_gateways_: list[ExtraGateway] = []
    for g in (data.get("extra_gateways") or []):
        env_name = g.get("api_key_env", "")
        lookup = _resolve_secret(env_name, g.get("api_key", ""))
        extra_gateways_.append(
            ExtraGateway(
                name=g["name"],
                base_url=os.path.expandvars(g["base_url"]),
                api_key=os.path.expandvars(lookup),
                api_key_env=env_name,
                timeout=float(g.get("timeout", 180)),
                default_model=g.get("default_model", ""),
                auth_scheme=str(g.get("auth_scheme", "bearer")).lower(),
                auth_param_name=_resolve_secret(
                    g.get("auth_param_env", ""), str(g.get("auth_param_name", ""))
                ),
                auth_param_env=str(g.get("auth_param_env", "")),
            )
        )

    _validate_extra_gateway_names(extra_gateways_)
    _validate_auth_schemes(extra_gateways_)

    roster = [
        ModelEntry(
            id=m["id"],
            label=m.get("label", m["id"]),
            role=m.get("role", "generalist"),
            provider=m.get("provider", "gateway"),
            local=bool(m.get("local", False)),
        )
        for m in (data.get("roster") or [])
    ]

    b = data.get("brain", {}) or {}
    brain_path = b.get("path", str(ROOT / "data" / "brain.db"))
    if not os.path.isabs(brain_path):
        brain_path = str(ROOT / brain_path)
    brain = BrainConfig(
        path=brain_path,
        prefer_local_for_learning=bool(b.get("prefer_local_for_learning", True)),
        allow_cloud_learning=bool(b.get("allow_cloud_learning", False)),
    )

    return AppConfig(
        gateway=gateway,
        locals=locals_,
        extra_gateways=extra_gateways_,
        roster=roster,
        brain=brain,
    )
