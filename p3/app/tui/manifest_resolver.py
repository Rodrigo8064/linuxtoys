"""Manifest resolution for the TUI.

Mirrors the validation pipeline of the CLI ``run_manifest_mode`` but never
prints, prompts, installs or touches the UI. It returns a :class:`ManifestPlan`
that the caller can review, confirm and execute.

``resolve_manifest`` is blocking (subprocesses and ``asyncio.run``), so it must
be called from a worker thread, never from the Textual event loop.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from app import homebrew_catalog
from app.manifest_helper import (
    _prepare_homebrew_manifest_entries,
    _select_manifest_source,
    appstream_cache,
    appstream_parser,
    check_flatpaks_async,
    check_package_exists,
    find_script_by_id,
    find_script_by_name,
    get_system_compat_keys,
    is_containerized,
    materialize_repo_script,
    script_is_compatible,
    script_is_container_compatible,
    valid_flatpak_id,
    valid_manifest_value,
    valid_package_name,
)

_ENTRY_PREFIXES = frozenset(
    {"script", "package", "pkg", "flatpak", "snap", "homebrew", "require"}
)
_SOURCE_OVERRIDES = frozenset({"native", "flatpak", "snap", "homebrew"})
_SUPPORTED_REQUIREMENTS = frozenset({"flathub"})
_FORMULA_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.@-]*")


@dataclass
class ManifestPlan:
    """Result of resolving a manifest against the current system.

    ``errors`` are blocking: if any is present nothing must be executed.
    ``warnings`` are items skipped for compatibility reasons.

    ``bootstrap`` is ``"flatpak"`` or ``"homebrew"`` when a prerequisite must
    be set up before the manifest can be resolved; the plan is then incomplete
    and must be discarded. Resolution never performs the bootstrap itself,
    because it may need ``sudo`` and therefore a real terminal.
    ``bootstrap_script`` holds the script that sets Homebrew up.
    """

    scripts: list[dict] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    snaps: list[str] = field(default_factory=list)
    flatpaks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    bootstrap: str | None = None
    bootstrap_script: dict | None = None

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def total(self) -> int:
        return (
            len(self.scripts)
            + len(self.packages)
            + len(self.snaps)
            + len(self.flatpaks)
        )


@dataclass
class _Entries:
    """Manifest lines grouped by how they were declared."""

    scripts: list[tuple[str, str]] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    snaps: list[str] = field(default_factory=list)
    flatpaks: list[str] = field(default_factory=list)
    homebrew: list[str] = field(default_factory=list)
    auto: list[str] = field(default_factory=list)
    requirements: set[str] = field(default_factory=set)


def resolve_manifest(names: list[str], translations=None) -> ManifestPlan:
    """Classify and validate manifest entries without executing anything."""
    return _ManifestResolver(translations).resolve(names)


def refresh_appstream_after_flathub() -> None:
    """Rebuild AppStream so Flathub entries are visible to the next pass.

    Mirrors the tail of the CLI's ``_bootstrap_flatpak_for_manifest``.
    """
    appstream_cache.refresh_cache(force=True)
    appstream_parser.clear_runtime_cache()


def _flathub_ready() -> bool:
    """Return True when Flatpak is installed and the Flathub remote exists."""
    if shutil.which("flatpak") is None:
        return False
    try:
        remote = subprocess.run(
            ["flatpak", "remote-list", "--columns=name"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
            env=os.environ.copy(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    remotes = {line.strip() for line in remote.stdout.splitlines()}
    return remote.returncode == 0 and "flathub" in remotes


def _split_script_source(value: str) -> tuple[str, str]:
    """Split ``id@source`` into ``(id, source)``; source is '' if absent."""
    script_id, separator, source = value.rpartition("@")
    if separator and source.casefold() in _SOURCE_OVERRIDES:
        return script_id, source.casefold()
    return value, ""


class _ManifestResolver:
    def __init__(self, translations=None) -> None:
        self._translations = translations
        self._plan = ManifestPlan()
        self._compat_keys = get_system_compat_keys()
        self._containerized = is_containerized()
        self._steamos = "steamos" in self._compat_keys
        self._auto_flatpaks: list[str] = []

    def resolve(self, names: list[str]) -> ManifestPlan:
        entries = self._parse(names)

        if not self._flatpak_available(entries):
            return self._plan
        if not self._add_homebrew(entries.homebrew):
            return self._plan

        for name in entries.packages:
            self._add_package(name)
        for name in entries.snaps:
            self._add_snap(name)

        self._add_explicit_scripts(entries.scripts)
        if self._plan.bootstrap:
            return self._plan
        for name in entries.auto:
            self._add_auto_item(name)

        self._add_flatpaks(entries.flatpaks + self._auto_flatpaks)

        self._plan.packages = list(dict.fromkeys(self._plan.packages))
        self._plan.snaps = list(dict.fromkeys(self._plan.snaps))
        unique_scripts: dict[tuple, dict] = {}
        for script in self._plan.scripts:
            key = (script.get("name"), script.get("path"))
            unique_scripts.setdefault(key, script)
        self._plan.scripts = list(unique_scripts.values())
        return self._plan

    # -- parsing -----------------------------------------------------------

    def _parse(self, names: list[str]) -> _Entries:
        entries = _Entries()
        for raw_name in names:
            prefix, separator, value = raw_name.partition(":")
            kind = prefix.lower()
            if not separator or kind not in _ENTRY_PREFIXES:
                # Unprefixed entries keep the CLI auto-detection.
                entries.auto.append(raw_name)
                continue

            value = value.strip()
            if not value:
                self._plan.errors.append(
                    f"Entrada vazia no manifesto: '{raw_name}'."
                )
            elif kind == "script":
                entries.scripts.append(_split_script_source(value))
            elif kind in {"package", "pkg"}:
                entries.packages.append(value)
            elif kind == "homebrew":
                entries.homebrew.append(value)
            elif kind == "snap":
                entries.snaps.append(value)
            elif kind == "require":
                requirement = value.casefold()
                if requirement in _SUPPORTED_REQUIREMENTS:
                    entries.requirements.add(requirement)
                else:
                    self._plan.errors.append(
                        f"Requisito de manifesto desconhecido: '{value}'."
                    )
            else:
                entries.flatpaks.append(value)
        return entries

    # -- prerequisites: flagged, never performed here ----------------------

    def _flatpak_available(self, entries: _Entries) -> bool:
        needs_flatpak = (
            "flathub" in entries.requirements
            or bool(entries.flatpaks)
            or any(source == "flatpak" for _id, source in entries.scripts)
        )
        if needs_flatpak and not _flathub_ready():
            self._plan.bootstrap = "flatpak"
            return False
        return True

    def _add_homebrew(self, names: list[str]) -> bool:
        """Validate formula names first, then check that Brew is present."""
        if not names:
            return True

        formulae = list(dict.fromkeys(names))
        invalid = [
            formula
            for formula in formulae
            if not valid_manifest_value(formula)
            or not _FORMULA_RE.fullmatch(formula)
        ]
        if invalid:
            self._plan.errors.extend(
                f"Nome de fórmula Homebrew inválido: {formula!r}."
                for formula in invalid
            )
            return False

        if not homebrew_catalog.enabled():
            brew_script = find_script_by_id("brew", self._translations)
            if brew_script is None:
                self._plan.errors.append(
                    "Não foi possível preparar o Homebrew exigido por este "
                    "manifesto: o script 'brew' não foi encontrado."
                )
            else:
                self._plan.bootstrap = "homebrew"
                self._plan.bootstrap_script = brew_script
            return False

        try:
            self._plan.scripts.extend(
                _prepare_homebrew_manifest_entries(
                    formulae, self._translations
                )
            )
        except ValueError as exc:
            self._plan.errors.append(str(exc))
            return False
        return True

    # -- packages / snaps / flatpaks ---------------------------------------

    def _add_package(self, name: str, *, auto: bool = False) -> None:
        if not valid_package_name(name):
            kind = "Item inseguro ou malformado" if auto else "Pacote inválido"
            self._plan.errors.append(f"{kind}: '{name}'.")
        elif self._steamos:
            # The CLI refuses native packages on SteamOS; say so up front.
            self._plan.warnings.append(
                f"Pacote '{name}' ignorado: pacotes nativos não são "
                "suportados no SteamOS."
            )
        elif check_package_exists(name):
            self._plan.packages.append(name)
        elif auto:
            self._plan.errors.append(
                f"'{name}' não é um script do LinuxToys nem um pacote "
                "disponível."
            )
        else:
            self._plan.errors.append(
                f"Pacote '{name}' não encontrado nos repositórios disponíveis."
            )

    def _add_snap(self, name: str) -> None:
        if valid_package_name(name):
            self._plan.snaps.append(name)
        else:
            self._plan.errors.append(f"Nome de snap inválido: '{name}'.")

    def _add_flatpaks(self, candidates: list[str]) -> None:
        """Validate IDs first, then check them all in a single async batch."""
        valid_ids = []
        for name in dict.fromkeys(candidates):
            if valid_flatpak_id(name):
                valid_ids.append(name)
            else:
                self._plan.errors.append(f"ID Flatpak inválido: '{name}'.")
        if not valid_ids:
            return

        found = asyncio.run(check_flatpaks_async(valid_ids))
        for name, exists in zip(valid_ids, found):
            if exists:
                self._plan.flatpaks.append(name)
            else:
                self._plan.errors.append(
                    f"Flatpak '{name}' não encontrado nos remotes "
                    "configurados."
                )

    # -- scripts -----------------------------------------------------------

    def _add_explicit_scripts(self, entries: list[tuple[str, str]]) -> None:
        """Resolve ``script:`` entries strictly through stable LinuxToys IDs."""
        resolved: list[tuple[str, str, dict]] = []
        unresolved: list[tuple[str, str]] = []
        for script_id, source in entries:
            info = find_script_by_id(script_id, self._translations)
            if info is None:
                unresolved.append((script_id, source))
            else:
                resolved.append((script_id, source, info))

        # A portable ID may point to a Flathub-only AppStream app that is not
        # visible on a fresh system: ask the caller to bootstrap and retry.
        if unresolved and not _flathub_ready():
            self._plan.bootstrap = "flatpak"
            return

        for script_id, _source in unresolved:
            self._plan.errors.append(
                f"ID do LinuxToys '{script_id}' não encontrado neste sistema."
            )

        for script_id, source, info in resolved:
            display = f"script:{script_id}"
            if source:
                info = _select_manifest_source(info, source)
                display = f"{display}@{source}"
                if info is None:
                    self._plan.errors.append(
                        f"O ID '{script_id}' não oferece a fonte '{source}' "
                        "neste sistema."
                    )
                    continue
            script = self._accept_script(info, display)
            if script is not None:
                self._plan.scripts.append(script)

    def _add_auto_item(self, name: str) -> None:
        info = find_script_by_name(name, self._translations)
        if info is None:
            # Not a LinuxToys/AppStream entry: try Flatpak, then package.
            if valid_flatpak_id(name):
                self._auto_flatpaks.append(name)
            else:
                self._add_package(name, auto=True)
            return

        script = self._accept_script(info, name)
        if script is not None:
            self._plan.scripts.append(script)

    def _accept_script(self, info: dict, display: str) -> dict | None:
        """Apply source-specific checks; return the runnable info or None.

        The returned dict is always the *original* info: Homebrew entries are
        materialized by the runner right before execution, and repository
        entries are materialized again there (the generated file is
        deterministic) so that ``is_repo_entry`` stays available to
        ``script_environment``.
        """
        if info.get("appstream_source") == "homebrew":
            if not homebrew_catalog.enabled():
                self._plan.errors.append(
                    f"Homebrew não está habilitado para '{display}'."
                )
                return None
            return info

        if info.get("is_repo_entry"):
            # Validate now so a bad entry aborts before anything runs.
            try:
                materialize_repo_script(info)
            except (ValueError, NotImplementedError, OSError) as exc:
                self._plan.errors.append(
                    f"Não foi possível preparar '{display}': {exc}"
                )
                return None
            return info

        path = info.get("path") or ""
        if not path or "://" in path:
            # Safety net: a virtual path that nothing can materialize would
            # crash the runner when it tries to open the file.
            self._plan.errors.append(
                f"'{display}' não possui um script executável neste sistema."
            )
            return None
        if not script_is_compatible(path, self._compat_keys):
            self._plan.warnings.append(
                f"'{display}' não é compatível com este sistema."
            )
            return None
        if self._containerized and not script_is_container_compatible(path):
            self._plan.warnings.append(
                f"'{display}' não é compatível com sistemas em container."
            )
            return None
        return info
