import re

from app.lang_utils import load_translations
from app.parser import (
    get_categories,
    get_scripts_for_category,
)
from app.search_helper import ScriptCache, SearchEngine

translations = load_translations()  # Auto-detect language from lang_utils
_search_index_cache: list[dict] | None = None
_category_scripts_cache: dict[str, list[dict]] = {}


def load_categories(translations) -> list[dict]:
    categories = get_categories(translations=translations)
    return categories


def make_widget_id(identifier: str) -> str:
    """Gera um id de widget válido e estável a partir de algo que já é
    único e independente de idioma (ex.: o path do script).
    NUNCA passar texto traduzido aqui — só chaves/paths estáveis."""
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", identifier)
    # Textual exige que o id comece com letra ou underscore
    if not slug or not (slug[0].isalpha() or slug[0] == "_"):
        slug = f"id_{slug}"
    return slug


def get_search_index():
    script_cache = ScriptCache()
    search_engine = SearchEngine(translations, script_cache)
    search_index_cache = script_cache.populate(translations)

    return search_engine


def search_scripts(query: str):
    search_engine = get_search_index()
    query = query.strip().lower()
    if not query:
        return []
    else:
        groups = search_engine.search(query)
        items = [
            result.item_info for group in groups for result in group["scripts"]
        ]
        return items


def get_scripts_for_category_cached(category_path: str) -> list[dict]:
    """Versão cacheada de get_scripts_for_category — calculada uma vez
    por categoria e reaproveitada. Evita revarrer o disco toda vez que
    o usuário entra na mesma categoria."""
    if category_path not in _category_scripts_cache:
        _category_scripts_cache[category_path] = get_scripts_for_category(
            category_path, translations=translations
        )
    return _category_scripts_cache[category_path]


def warm_category_cache() -> None:
    """Pré-computa os scripts de TODAS as categorias de topo em
    background — assim, entrar em qualquer uma delas já está pronto
    quando o usuário chegar lá."""
    categories = load_categories(translations)
    for category in categories:
        get_scripts_for_category_cached(category["path"])
