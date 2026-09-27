import re
import threading

from app.lang_utils import load_translations
from app.search_helper import (
    CategoryCache,
    ScriptCache,
    SearchEngine,
    create_search_engine,
)

translations = load_translations()

_category_cache: CategoryCache | None = None
_script_cache: ScriptCache | None = None
_search_engine: SearchEngine | None = None
_cache_lock = threading.Lock()


def make_widget_id(identifier: str) -> str:
    """Gera um id de widget válido e estável a partir de algo que já é
    único e independente de idioma (ex.: o path do script).
    NUNCA passar texto traduzido aqui — só chaves/paths estáveis."""
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", identifier)
    # Textual exige que o id comece com letra ou underscore
    if not slug or not (slug[0].isalpha() or slug[0] == "_"):
        slug = f"id_{slug}"
    return slug


def is_search_ready() -> bool:
    return _search_engine is not None and _category_cache is not None


def is_removable(script_name: str, script_path: str) -> bool:
    global _script_cache
    script_cache = _script_cache
    script_info = {"name": script_name, "path": script_path}
    return script_cache.is_script_removable(script_info)


def invalidate_search_caches() -> None:
    """Invalidates cached search and category structures."""
    global _category_cache, _script_cache, _search_engine
    with _cache_lock:
        _category_cache = None
        _script_cache = None
        _search_engine = None


def warm_search_and_category_index(trans=None) -> SearchEngine:
    """Populates CategoryCache concurrently, links ScriptCache in memory,
    and warms the Rust SearchIndex. Thread-safe and reusable.
    """
    global _category_cache, _script_cache, _search_engine
    active_translations = trans or translations

    with _cache_lock:
        if _search_engine is not None:
            return _search_engine

        # 1. Parse categories concurrently (ThreadPoolExecutor)
        cat_cache = CategoryCache()
        cat_cache.populate(active_translations, max_workers=4)

        # 2. Build script cache directly from category data without re-reading the filesystem
        scr_cache = ScriptCache()
        scr_cache.populate_from_category_cache(cat_cache)

        # 3. Create SearchEngine and warm the Rust index
        engine = create_search_engine(active_translations, scr_cache)
        engine._ensure_rust_search_index()

        _category_cache = cat_cache
        _script_cache = scr_cache
        _search_engine = engine

        return _search_engine


def get_scripts_for_category_cached(category_path: str) -> list[dict]:
    """Retrieves scripts from the warmed CategoryCache if available,
    falling back to parser only if not yet populated.
    """
    if _category_cache is not None and _category_cache.is_populated:
        return _category_cache.get_scripts_for_category(category_path)
    from app.parser import get_scripts_for_category

    return get_scripts_for_category(category_path, translations=translations)


def search_scripts_fast(query: str) -> list[dict]:
    """Fast search leveraging the pre-warmed Rust index."""
    if not query.strip() or _search_engine is None:
        return []

    groups = _search_engine.search(query.strip())
    # Flatten items or return grouped
    return [
        result.item_info for group in groups for result in group["scripts"]
    ]


def get_categories(trans=None) -> list[dict]:
    """Retrieves top-level categories.
    Returns from CategoryCache if populated, otherwise falls back to parser.
    """
    if _category_cache is not None and _category_cache.is_populated:
        return _category_cache.get_categories()
    from app.parser import get_categories as parser_get_categories

    return parser_get_categories(translations=trans or translations)
