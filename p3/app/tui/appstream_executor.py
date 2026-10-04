import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from app import repo_parser
from app.appstream_resolver import resolve_native_appstream_package
from app.library_loader import _appstream_phase_lines

_OVERRIDES_DIR = "/tmp/linuxtoys/appstream-overrides"

AUR_SECURITY_OK = 0
AUR_SECURITY_UNAVAILABLE = 125
AUR_SECURITY_BLOCKED = 126
_AUR_SCAN_MAX_FILE = 1024 * 1024
_AUR_OBFUSCATION_RULES = (
    (
        "encoded data is decoded and executed by a shell",
        re.compile(
            r"""(?ix)
            (?:base64\s+(?:-[A-Za-z]*d|--decode)|openssl\s+(?:enc\s+)?-[A-Za-z0-9_-]*d|
               xxd\s+-r|(?:gzip|bzip2|xz)\s+-d[c]?|python[23]?\s+-c\s+[^;\n]*(?:b64decode|fromhex))
            [^;\n|]{0,240}\|\s*(?:ba|z|k|da)?sh\b
            """
        ),
    ),
    (
        "decoded or generated content is passed to eval",
        re.compile(
            r"""(?ix)
            \beval\b[^\n]{0,300}
            (?:base64|b64decode|fromhex|xxd\s+-r|openssl\s+(?:enc\s+)?-|
               printf\s+['"][^'"]*\\x[0-9a-f]{2})
            """
        ),
    ),
    (
        "eval executes command substitution",
        re.compile(r"""(?ix)\beval\s+["']?\s*\$\(\s*[^)\n]{4,}\)"""),
    ),
    (
        "a large encoded payload is embedded in the build script",
        re.compile(
            r"""(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{600,}={0,2}(?![A-Za-z0-9+/])"""
        ),
    ),
    (
        "a large hexadecimal payload is embedded in the build script",
        re.compile(r"""(?i)(?<![0-9a-f])(?:[0-9a-f]{2}){300,}(?![0-9a-f])"""),
    ),
    (
        "long hexadecimal escape sequence suggests hidden executable text",
        re.compile(r"""(?i)(?:\\x[0-9a-f]{2}){40,}"""),
    ),
)


def _write_temp_script(lines: list[str], prefix: str) -> str:
    os.makedirs(_OVERRIDES_DIR, mode=0o700, exist_ok=True)
    fd, path = tempfile.mkstemp(
        prefix=prefix, suffix=".sh", dir=_OVERRIDES_DIR, text=True
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("#!/usr/bin/env bash\n")
        handle.write("\n".join(lines))
        handle.write("\n")
    os.chmod(path, 0o700)
    return path


def _dependency_lines(dependencies) -> list[str]:
    """Porta direta de _new_dependency_script, mas devolvendo linhas em vez
    de já escrever um arquivo — a montagem final acontece em build_install_script."""
    if not isinstance(dependencies, list):
        return []

    lines = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            continue
        dependency_type = dependency.get("type")
        packages = dependency.get("packages")
        if not isinstance(packages, list):
            continue
        for package in packages:
            package = str(package or "").strip()
            if not package:
                continue
            if dependency_type == "native":
                lines.append(f"pkg_install {shlex.quote(package)}")
            elif dependency_type == "flathub":
                lines.append(f"pkg_flat {shlex.quote(package)}")
            elif dependency_type == "snap":
                lines.append(f"pkg_snap {shlex.quote(package)}")
    return lines


def _override_lines(overrides: dict, phase: str) -> list[str]:
    """Porta direta de _new_override_script, devolvendo linhas."""
    if not isinstance(overrides, dict):
        return []

    lines = []
    if phase in ("pre", "post"):
        value = overrides.get(phase)
        if isinstance(value, str) and value.strip():
            lines.append(value.strip())
        elif isinstance(value, dict):
            script = str(value.get("script", "") or "").strip()
            if script:
                lines.append(f"run_list_hook {shlex.quote(script)}")
    elif phase == "flatpak":
        values = overrides.get("flatpak")
        if isinstance(values, list):
            for override in values:
                if not isinstance(override, dict):
                    continue
                lines.append(
                    "flatpak_override "
                    f"{shlex.quote(str(override.get('scope', '')).strip())} "
                    f"{shlex.quote(str(override.get('type', '')).strip())} "
                    f"{shlex.quote(str(override.get('setting', '')).strip())} "
                    f"{shlex.quote(str(override.get('target', '')).strip())}"
                )
    return lines


def _resolve_native_package_env(script_info: dict) -> str | None:
    """Porta da lógica de resolução de pacote nativo único em _run_job."""
    if script_info.get("appstream_source") != "native":
        return None

    package_value = script_info.get("package-name")
    if isinstance(package_value, str):
        packages = [package_value.strip()] if package_value.strip() else []
    elif isinstance(package_value, (list, tuple)):
        packages = [str(v).strip() for v in package_value if str(v).strip()]
    else:
        packages = []

    if len(packages) != 1:
        return None

    return resolve_native_appstream_package(
        packages[0],
        component_id=script_info.get("appstream_id"),
        desktop_id=script_info.get("appstream_launchable"),
        name=script_info.get("name"),
    )


def build_install_script(entry: dict) -> str:
    """Monta o pipeline completo (pre -> deps -> install -> flatpak
    overrides -> post) em UM arquivo .sh. Cada fase é checada logo após
    seu último comando, preservando a semântica de 'a primeira fase que
    falhar interrompe o resto' do AppStreamRunner._run_job original.
    """
    install_path = repo_parser.create_install_script(entry)
    try:
        install_text = Path(install_path).read_text(encoding="utf-8")
    finally:
        try:
            os.remove(install_path)
        except OSError:
            pass

    lines = install_text.splitlines()
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    install_body = "\n".join(lines).strip()

    pre, dependencies, flatpak, post = _appstream_phase_lines(entry)

    phases = []
    if pre:
        phases.append("\n".join(pre))
    if dependencies:
        phases.append("\n".join(dependencies))
    phases.append(install_body)
    if flatpak:
        phases.append("\n".join(flatpak))
    if post:
        phases.append("\n".join(post))

    gate = "\nif [ $? -ne 0 ]; then exit $?; fi\n"
    script_body = gate.join(phases)

    os.makedirs(_OVERRIDES_DIR, mode=0o700, exist_ok=True)
    fd, path = tempfile.mkstemp(
        prefix="appstream-install-",
        suffix=".sh",
        dir=_OVERRIDES_DIR,
        text=True,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("#!/usr/bin/env bash\n\n")
        handle.write(script_body)
        handle.write("\n")
    os.chmod(path, 0o700)
    return path


def build_flatpak_extension_command(info: dict, remove: bool) -> list[str]:
    ref = str(info.get("flatpak_ref") or "").strip()
    if not ref:
        raise ValueError("Extension has no flatpak_ref")

    scope = str(info.get("flatpak_scope") or "").strip()
    installation = str(info.get("flatpak_installation") or "").strip()
    if scope == "user":
        scope_args = ["--user"]
    elif installation and installation != "default":
        scope_args = [f"--installation={installation}"]
    else:
        scope_args = ["--system"]

    argv = ["flatpak", *scope_args]
    if remove:
        argv += ["uninstall", "-y", ref]
    else:
        remote = (
            str(info.get("flatpak_remote") or "flathub").strip() or "flathub"
        )
        argv += ["install", "-y", remote, ref]
    return argv


def build_snap_revert_command(info: dict) -> list[str]:
    snap_name = str(info.get("snap_name") or "").strip()
    if not snap_name:
        raise ValueError("Entry has no snap_name")
    return ["pkg_snap_revert", snap_name]


def _scan_aur_text_for_obfuscation(filename: str, text: str) -> list[str]:
    findings = []
    for description, pattern in _AUR_OBFUSCATION_RULES:
        match = pattern.search(text)
        if match is None:
            continue
        line = text.count("\n", 0, match.start()) + 1
        findings.append(f"{filename}:{line}: {description}")
    return findings


def check_aur_package_security(package: str) -> tuple[int, str]:
    """Porte direto de AppStreamRunner._verify_aur_package_sources.
    Clona o repositório AUR raso e inspeciona estaticamente o PKGBUILD
    e os hooks *.install — nada do repositório é executado.
    """
    git = shutil.which("git")
    if not git:
        return (
            AUR_SECURITY_UNAVAILABLE,
            "Git is required to verify the AUR package sources.",
        )

    safe_package = str(package or "").strip()
    if not safe_package or not re.fullmatch(
        r"[A-Za-z0-9@._+:-]+", safe_package
    ):
        return (
            AUR_SECURITY_UNAVAILABLE,
            "The AUR package name could not be safely verified.",
        )

    temp_root = tempfile.mkdtemp(prefix="linuxtoys-aur-verify-")
    repo_dir = os.path.join(temp_root, "repo")
    try:
        repository = f"https://aur.archlinux.org/{safe_package}.git"
        try:
            result = subprocess.run(
                [
                    git,
                    "-c",
                    "protocol.file.allow=never",
                    "clone",
                    "--quiet",
                    "--depth",
                    "1",
                    "--no-tags",
                    "--config",
                    "core.hooksPath=/dev/null",
                    repository,
                    repo_dir,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=45,
                check=False,
                env={
                    **os.environ,
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return (
                AUR_SECURITY_UNAVAILABLE,
                f"Could not fetch the AUR repository for verification: {exc}",
            )

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            if len(detail) > 1000:
                detail = detail[-1000:]
            return (
                AUR_SECURITY_UNAVAILABLE,
                "Could not fetch the AUR repository for verification."
                + (f"\n\n{detail}" if detail else ""),
            )

        candidates = []
        pkgbuild = os.path.join(repo_dir, "PKGBUILD")
        if os.path.isfile(pkgbuild) and not os.path.islink(pkgbuild):
            candidates.append(pkgbuild)

        for root, dirs, files in os.walk(repo_dir, followlinks=False):
            dirs[:] = [d for d in dirs if d != ".git"]
            for filename in files:
                if not filename.endswith(".install"):
                    continue
                path = os.path.join(root, filename)
                if os.path.isfile(path) and not os.path.islink(path):
                    candidates.append(path)

        if not candidates or pkgbuild not in candidates:
            return (
                AUR_SECURITY_UNAVAILABLE,
                "The AUR repository did not contain a readable PKGBUILD.",
            )

        findings = []
        for path in candidates:
            try:
                size = os.path.getsize(path)
                if size > _AUR_SCAN_MAX_FILE:
                    relative = os.path.relpath(path, repo_dir)
                    findings.append(
                        f"{relative}: file is too large to safely inspect "
                        f"({size} bytes)"
                    )
                    continue
                with open(
                    path, "r", encoding="utf-8", errors="replace"
                ) as handle:
                    source = handle.read(_AUR_SCAN_MAX_FILE + 1)
            except OSError as exc:
                return (
                    AUR_SECURITY_UNAVAILABLE,
                    f"Could not read an AUR source file for verification: {exc}",
                )

            relative = os.path.relpath(path, repo_dir)
            findings.extend(_scan_aur_text_for_obfuscation(relative, source))

        if findings:
            return (AUR_SECURITY_BLOCKED, "\n".join(findings[:20]))
        return (AUR_SECURITY_OK, "")
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def build_aur_install_script(package: str) -> str:
    """Igual a _run_aur_job: só pkg_install <package>, sem create_install_script."""
    os.makedirs(_OVERRIDES_DIR, mode=0o700, exist_ok=True)
    fd, path = tempfile.mkstemp(
        prefix="aur-install-", suffix=".sh", dir=_OVERRIDES_DIR, text=True
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("#!/usr/bin/env bash\n")
        handle.write(f"pkg_install {shlex.quote(package)}\n")
    os.chmod(path, 0o700)
    return path
