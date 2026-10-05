"""Which coding agents live on this machine, and which ones Synergo can hook.

A registry, not a plugin system: each known agent is one `AgentSpec` saying how
to spot it (an executable on PATH and/or a config directory in the home
directory) and, when Synergo ships a hook adapter for it, how to install that
adapter. Adding an adapter for another agent is one `HookAdapter` on its entry.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


@dataclass(frozen=True)
class HookScope:
    """Where hooks go: one project's settings, or the user's global settings."""

    root: Optional[Path] = None

    @classmethod
    def project(cls, root: Path) -> HookScope:
        return cls(root=Path(root))

    @classmethod
    def user(cls) -> HookScope:
        return cls(root=None)

    @property
    def is_global(self) -> bool:
        return self.root is None


@dataclass(frozen=True)
class InstallResult:
    path: Path
    # Hook events that were added; empty means everything was already there.
    added: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.added)


@dataclass(frozen=True)
class HookAdapter:
    settings_path: Callable[[HookScope], Path]
    install: Callable[[HookScope], InstallResult]
    is_installed: Callable[[HookScope], bool]


@dataclass(frozen=True)
class AgentSpec:
    id: str
    label: str
    executables: tuple[str, ...] = ()
    # Relative to the home directory.
    config_dirs: tuple[str, ...] = ()
    adapter: Optional[HookAdapter] = None

    @property
    def has_adapter(self) -> bool:
        return self.adapter is not None

    def install(self, scope: HookScope) -> InstallResult:
        if self.adapter is None:
            raise LookupError(f"Synergo has no hook adapter for {self.label} yet")
        return self.adapter.install(scope)

    def is_installed(self, scope: HookScope) -> bool:
        return self.adapter is not None and self.adapter.is_installed(scope)


@dataclass(frozen=True)
class DetectedAgent:
    spec: AgentSpec
    executable: Optional[str] = None
    config_dir: Optional[Path] = None

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def label(self) -> str:
        return self.spec.label

    @property
    def has_adapter(self) -> bool:
        return self.spec.has_adapter

    @property
    def evidence(self) -> str:
        """How it was found, for the user: `claude on PATH`, `~/.codex`."""
        if self.executable:
            return f"{Path(self.executable).stem} on PATH"
        if self.config_dir is not None:
            return f"~/{self.config_dir.name}"
        return "detected"


# --- claude-code adapter -----------------------------------------------------
# Delegates to the merge logic in synergo.cli (imported lazily: cli imports this
# module lazily too, and the hook path must never pay for either).


def _claude_settings_path(scope: HookScope) -> Path:
    if scope.root is None:
        return home() / ".claude" / "settings.json"
    return scope.root / ".claude" / "settings.json"


def _claude_install(scope: HookScope) -> InstallResult:
    from synergo import cli
    from synergo.adapters import claude_code

    path = _claude_settings_path(scope)
    settings = cli.load_settings(path)  # raises on unparseable files: never clobber
    added = cli.merge_hooks(settings, claude_code.hook_settings())
    if added:
        cli.save_settings(path, settings)
    return InstallResult(path=path, added=tuple(added))


def _claude_is_installed(scope: HookScope) -> bool:
    from synergo import cli
    from synergo.adapters import claude_code

    try:
        settings = cli.load_settings(_claude_settings_path(scope))
    except (cli.ConfigError, OSError):
        return False
    return cli.has_synergo_hooks(settings, tuple(claude_code.hook_settings()))


CLAUDE_CODE_ADAPTER = HookAdapter(
    settings_path=_claude_settings_path,
    install=_claude_install,
    is_installed=_claude_is_installed,
)


# --- registry ----------------------------------------------------------------

PRIMARY_AGENT = "claude-code"

KNOWN_AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        id="claude-code",
        label="Claude Code",
        executables=("claude",),
        config_dirs=(".claude",),
        adapter=CLAUDE_CODE_ADAPTER,
    ),
    AgentSpec(id="codex", label="Codex", executables=("codex",), config_dirs=(".codex",)),
    AgentSpec(
        id="cursor",
        label="Cursor",
        executables=("cursor-agent", "cursor"),
        config_dirs=(".cursor",),
    ),
    AgentSpec(id="gemini-cli", label="Gemini CLI", executables=("gemini",), config_dirs=(".gemini",)),
    AgentSpec(id="aider", label="Aider", executables=("aider",)),
)


def get_agent(agent_id: str) -> AgentSpec:
    for spec in KNOWN_AGENTS:
        if spec.id == agent_id:
            return spec
    raise KeyError(agent_id)


# Seams for tests: detection must not depend on the machine running the suite.
def which(name: str) -> Optional[str]:
    return shutil.which(name)


def home() -> Path:
    return Path.home()


def detect(spec: AgentSpec) -> Optional[DetectedAgent]:
    executable = next((found for name in spec.executables if (found := which(name))), None)
    config_dir: Optional[Path] = None
    for name in spec.config_dirs:
        candidate = home() / name
        try:
            if candidate.is_dir():
                config_dir = candidate
                break
        except OSError:
            continue
    if executable is None and config_dir is None:
        return None
    return DetectedAgent(spec=spec, executable=executable, config_dir=config_dir)


def detect_agents() -> list[DetectedAgent]:
    """Known agents found on this machine, in registry order."""
    return [found for spec in KNOWN_AGENTS if (found := detect(spec)) is not None]
