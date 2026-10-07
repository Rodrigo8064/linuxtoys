from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass

from app.skills_fetcher import fetch_popular as _fetch_popular
from app.skills_fetcher import search_skills as _search_skills

SKILLS_SH_URL = "https://skills.sh"

LIST_TIMEOUT = 30
INSTALL_TIMEOUT = 120
REMOVE_TIMEOUT = 60

# (diretório de configuração, id do agente usado pelo `npx skills -a`)
_AGENT_DIRS: tuple[tuple[tuple[str, ...], str], ...] = (
    ((".claude",), "claude-code"),
    ((".codex",), "codex"),
    ((".config", "opencode"), "opencode"),
    ((".cursor",), "cursor"),
    ((".windsurf",), "windsurf"),
    ((".gemini",), "gemini-cli"),
    ((".agents",), "cline"),
    ((".roo",), "roo"),
    ((".trae",), "trae"),
    ((".kilocode",), "kilo"),
    ((".factory",), "droid"),
    ((".copilot",), "github-copilot"),
)

_MOCK_INSTALLED = [
    {
        "name": "react-expert",
        "agents": ["claude-code", "opencode"],
        "source": "ai-agents",
    },
    {"name": "python-debugger", "agents": ["claude-code"], "source": "devs"},
    {
        "name": "docker-helper",
        "agents": ["codex", "claude-code"],
        "source": "devs",
    },
    {
        "name": "git-workflow",
        "agents": ["opencode", "cline"],
        "source": "ai-agents",
    },
    {"name": "test-generator", "agents": ["claude-code"], "source": "devs"},
]


# --- Erros de domínio (a UI traduz para mensagens) ---------------------------


class SkillsError(Exception):
    """Base de todos os erros do domínio de skills."""


class NpxNotFoundError(SkillsError):
    """`npx` (Node.js) não está disponível no PATH."""


class MissingSourceError(SkillsError):
    """A skill não possui `source`/`skillId` suficientes para a operação."""


class CommandError(SkillsError):
    """Um comando npx falhou; `str(e)` contém a mensagem bruta."""


@dataclass(frozen=True)
class OpResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def message(self) -> str:
        return self.stdout or self.stderr


# --- Utilidades --------------------------------------------------------------


def is_dev_mode() -> bool:
    return os.environ.get("DEV_MODE") == "1"


def format_installs(count) -> str:
    """1200 -> '1.2K', 3_500_000 -> '3.5M'."""
    try:
        count = int(count)
    except (TypeError, ValueError):
        return "0"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


def detect_agents() -> list[str]:
    """Agentes de IA detectados pela presença do diretório de configuração."""
    home = os.path.expanduser("~")
    return [
        agent
        for parts, agent in _AGENT_DIRS
        if os.path.isdir(os.path.join(home, *parts))
    ]


def find_npx() -> str | None:
    return shutil.which("npx")


def skill_url(skill: dict) -> str | None:
    source = skill.get("source", "")
    skill_id = skill.get("skillId") or skill.get("id", "")
    if not source or not skill_id:
        return None
    return f"{SKILLS_SH_URL}/{source}/{skill_id}"


def open_skill_url(skill: dict) -> bool:
    """Abre a página da skill no navegador. False se não der para montar a URL."""
    url = skill_url(skill)
    if not url:
        return False
    try:
        subprocess.Popen(
            ["xdg-open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return True


# --- Catálogo (skills.sh) ----------------------------------------------------


def _normalize(raw: dict) -> dict:
    return {
        "id": raw.get("id", ""),
        "skillId": raw.get("skillId", ""),
        "name": raw.get("name", ""),
        "installs": raw.get("installs", 0),
        "source": raw.get("source", ""),
    }


def load_popular(limit: int = 50) -> list[dict]:
    return [_normalize(s) for s in _fetch_popular(limit).get("skills", [])]


def search(query: str, limit: int = 20) -> list[dict]:
    return [
        _normalize(s) for s in _search_skills(query, limit).get("skills", [])
    ]


# --- npx ---------------------------------------------------------------------


def _run_npx(args: tuple[str, ...], timeout: int) -> OpResult:
    npx = find_npx()
    if not npx:
        raise NpxNotFoundError
    try:
        r = subprocess.run(
            [npx, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return OpResult(False, timed_out=True)
    except OSError as e:
        return OpResult(False, stderr=str(e))
    return OpResult(r.returncode == 0, r.stdout.strip(), r.stderr.strip())


def install_skill(skill: dict, agent: str) -> OpResult:
    source = skill.get("source", "")
    slug = skill.get("skillId") or skill.get("id", "")
    if not source or not slug:
        raise MissingSourceError
    return _run_npx(
        ("skills", "add", source, "--skill", slug, "-a", agent, "-g", "-y"),
        INSTALL_TIMEOUT,
    )


def list_installed() -> list[dict]:
    if is_dev_mode():
        return list(_MOCK_INSTALLED)
    res = _run_npx(("skills", "list", "--json", "-g"), LIST_TIMEOUT)
    if not res.ok:
        raise CommandError(res.stderr or "timeout")
    try:
        data = json.loads(res.stdout or "[]")
    except json.JSONDecodeError as e:
        raise CommandError(str(e)) from e
    return data if isinstance(data, list) else []


def remove_skill(skill: dict) -> OpResult:
    """Remove a skill de cada agente vinculado (ou globalmente, se não houver)."""
    name = skill.get("name", "")
    agents = skill.get("agents", [])
    if not name:
        raise MissingSourceError

    if agents:
        # Mesmo mapeamento "best effort" da GUI: nome de exibição -> id.
        commands = [
            (
                "skills",
                "remove",
                name,
                "-a",
                a.lower().replace(" ", "-"),
                "-g",
                "-y",
            )
            for a in agents
        ]
    else:
        commands = [("skills", "remove", name, "-g", "-y")]

    errors: list[str] = []
    for cmd in commands:
        res = _run_npx(cmd, REMOVE_TIMEOUT)
        if not res.ok:
            errors.append(
                res.stderr or ("timeout" if res.timed_out else "error")
            )
    return OpResult(not errors, stderr="\n".join(errors))
