import re

from app.lang_utils import load_translations
from app.parser import get_all_scripts_recursive, get_categories

translations = load_translations()  # Auto-detect language from lang_utils
_search_index_cache: list[dict] | None = None


def load_categories(translations) -> list[dict]:
    categories = get_categories(translations=translations)
    return categories


def slugify(texto: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", texto.lower())


def get_search_index() -> list[dict]:
    """Achata todos os scripts de todas as categorias (recursivamente)
    numa lista única, pra busca por nome. Calculado uma vez só e
    reaproveitado — evita varrer o sistema de arquivos a cada tecla."""
    global _search_index_cache
    if _search_index_cache is None:
        categories = load_categories(translations)
        _search_index_cache = []
        for category in categories:
            _search_index_cache.extend(
                get_all_scripts_recursive(
                    category["path"], translations=translations
                )
            )
    return _search_index_cache


def search_scripts(query: str) -> list[dict]:
    query = query.strip().lower()
    if not query:
        return []
    return [
        item for item in get_search_index() if query in item["name"].lower()
    ]
