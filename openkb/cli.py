"""OpenKB CLI — command-line interface for the knowledge base workflow."""

from __future__ import annotations

# Silence import-time warnings (e.g. pydub's missing-ffmpeg warning emitted
# when markitdown pulls it in). markitdown later clobbers the filters during
# its own import, so we re-apply after all imports below.
import warnings

warnings.filterwarnings("ignore")

import asyncio
import json
import logging
import shutil
import sys
from functools import wraps
from pathlib import Path
from typing import Literal

import os

from agents import set_tracing_disabled

set_tracing_disabled(True)
# Use local model cost map — skip fetching from GitHub on every invocation
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import click


# Silence LiteLLM's "could not pre-load <aws-service> response stream
# shape" warnings — they fire at import time when ``botocore`` isn't
# installed, but botocore is only needed for AWS Bedrock / SageMaker
# users. Filter must be attached before ``import litellm`` runs.
class _SuppressLiteLLMPreloadWarnings(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "could not pre-load" not in record.getMessage()


logging.getLogger("LiteLLM").addFilter(_SuppressLiteLLMPreloadWarnings())

import litellm

litellm.suppress_debug_info = True
from dotenv import load_dotenv

from openkb.agent.compiler import DEFAULT_COMPILE_CONCURRENCY, compile_long_doc
from openkb.config import (
    DEFAULT_CONFIG,
    resolve_effective_config,
    load_global_config,
    register_kb,
    resolve_concurrency,
    set_extra_headers,
    resolve_parallel_tool_calls,
    set_parallel_tool_calls,
    set_timeout,
    resolve_per_request_overrides,
)
from openkb.converter import (
    _registry_path,
    resolve_doc_name_from_key,
)
from openkb.indexer import (
    _cloud_display_stem,
    _write_long_doc_artifacts,
    prepare_cloud_import,
)
from openkb.locks import atomic_write_text, kb_ingest_lock, kb_read_lock
from openkb.log import append_log
from openkb.application.documents import (
    _run_compile_with_retry,
    _snapshot_add_paths,
)
from openkb.application import documents as document_use_cases
from openkb.application.knowledge_bases import display_document_type as _display_type
from openkb.schema import PAGE_CONTENT_DIRS
from openkb.application.knowledge_bases import initialize_kb

# Suppress warnings after all imports — markitdown overrides filters at import time
import warnings

warnings.filterwarnings("ignore")

load_dotenv()  # load from cwd (covers running inside the KB dir)

logger = logging.getLogger(__name__)


_KNOWN_PROVIDER_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "MISTRAL_API_KEY",
    "MOONSHOT_API_KEY",
    "ZHIPUAI_API_KEY",
    "DASHSCOPE_API_KEY",
)

# Providers that authenticate via OAuth device flow (subscription login
# handled by LiteLLM itself) — no API key env var is needed, so the
# missing-key warning would be a false alarm for them.
_OAUTH_PROVIDERS = {"chatgpt", "github_copilot"}


def _extract_provider(model: str) -> str | None:
    """Extract the LiteLLM provider name from a model string.

    ``model`` uses ``provider/model`` LiteLLM format.
    OpenAI models can omit the prefix; default to ``"openai"``.
    """
    model = model.strip()
    if not model:
        return None
    if "/" in model:
        return model.split("/")[0].lower()
    return "openai"


def _apply_litellm_settings(settings: dict) -> None:
    """Set each ``litellm:`` key verbatim onto the litellm module (process-wide
    globals, so they reach every LiteLLM call). Skips with a warning a key the
    installed litellm doesn't define, or one that is a litellm function (e.g.
    ``completion``) since overwriting it would break later calls. Applied, never
    reset — the values persist for the life of the process.
    """
    for key, value in settings.items():
        if not hasattr(litellm, key):
            logger.warning(
                "config: LiteLLM has no setting %r — ignoring it "
                "(check the spelling or your installed litellm version).",
                key,
            )
            continue
        if callable(getattr(litellm, key)):
            logger.warning(
                "config: 'litellm.%s' is a LiteLLM function, not a setting — "
                "refusing to overwrite it from the litellm: config block.",
                key,
            )
            continue
        setattr(litellm, key, value)


def _setup_llm_key(kb_dir: Path | None = None) -> None:
    """Set LiteLLM API key from LLM_API_KEY env var if present.

    Load order (override=False, so first one wins):
    1. System environment variables (already set)
    2. KB-local .env  (kb_dir/.env)
    3. Global .env    (~/.config/openkb/.env)

    Also propagates to provider-specific env vars (OPENAI_API_KEY, etc.)
    so that the Agents SDK litellm provider can pick them up.
    Provider is auto-detected from the KB config when available; otherwise
    a common provider set is used as a fallback.
    """
    if kb_dir is not None:
        env_file = kb_dir / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)

    from openkb.config import GLOBAL_CONFIG_DIR

    global_env = GLOBAL_CONFIG_DIR / ".env"
    if global_env.exists():
        load_dotenv(global_env, override=False)

    api_key = os.environ.get("LLM_API_KEY", "")

    # Try to resolve the active provider, extra headers, and request timeout
    # from the KB config
    provider: str | None = None
    extra_headers: dict[str, str] = {}
    timeout: float | None = None
    parallel_tool_calls: bool | None = None
    parallel_tool_calls_explicit = False
    litellm_settings: dict = {}
    if kb_dir is not None:
        # Resolve model the same way the command bodies do (DEFAULT -> global.yaml
        # -> KB config.yaml) so provider extraction sees the effective, global-
        # layered model. Reading KB config.yaml alone would miss a global-only
        # default model (or fall back to DEFAULT_CONFIG's model) and derive the
        # wrong provider. resolve_effective_config handles a missing config.yaml
        # internally, so no config_path.exists() gate is needed.
        config = resolve_effective_config(kb_dir)[0]
        model = config.get("model", DEFAULT_CONFIG["model"])
        provider = _extract_provider(str(model))
        extra_headers, timeout, litellm_settings = resolve_per_request_overrides(config)
        parallel_tool_calls, parallel_tool_calls_explicit = resolve_parallel_tool_calls(config)
    set_extra_headers(extra_headers)
    set_timeout(timeout)
    set_parallel_tool_calls(parallel_tool_calls, parallel_tool_calls_explicit)
    _apply_litellm_settings(litellm_settings)

    if not api_key:
        # Check if any provider key is already set. OAuth-based providers
        # (ChatGPT subscription, GitHub Copilot) don't use API keys at all,
        # so the warning is skipped for them.
        check_keys = (f"{provider.upper()}_API_KEY",) if provider else _KNOWN_PROVIDER_KEYS
        has_key = any(os.environ.get(k) for k in check_keys)
        if not has_key and provider not in _OAUTH_PROVIDERS:
            click.echo(
                "Warning: No LLM API key found. Set one of:\n"
                f"  1. {kb_dir / '.env' if kb_dir else '<kb_dir>/.env'} — LLM_API_KEY=sk-...\n"
                f"  2. {GLOBAL_CONFIG_DIR / '.env'} — LLM_API_KEY=sk-...\n"
                "  3. Export LLM_API_KEY in your shell profile"
            )
    else:
        litellm.api_key = api_key

        # Dynamically set the provider-specific env var when possible
        if provider:
            provider_env = f"{provider.upper()}_API_KEY"
            if not os.environ.get(provider_env):
                os.environ[provider_env] = api_key

        # Fallback: also set common provider keys so multi-provider
        # configs (e.g. PageIndex Cloud) still work
        for env_var in _KNOWN_PROVIDER_KEYS:
            if not os.environ.get(env_var):
                os.environ[env_var] = api_key


from openkb.inputs import SUPPORTED_EXTENSIONS

# Map raw doc types to display types
_TYPE_DISPLAY_MAP = {
    "long_pdf": "pageindex",
    "pageindex_cloud": "pageindex",
}

# Registry types that were compiled via the long-doc pipeline (tree + per-page
# JSON source), as opposed to short docs (markdown source). Both the local
# long-PDF type and cloud imports belong here — they share the long-doc
# summary/source layout and recompile path.
from openkb.application import recompilation as recompilation_use_cases

_is_long_doc = recompilation_use_cases.is_long_doc
_LONG_DOC_TYPES = recompilation_use_cases.LONG_DOC_TYPES


_SHORT_DOC_TYPES = {
    "pdf",
    "docx",
    "md",
    "markdown",
    "html",
    "htm",
    "txt",
    "csv",
    "pptx",
    "xlsx",
    "xls",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_kb_dir(override: Path | None = None) -> Path | None:
    """Find the KB root: explicit override → walk up from cwd → global default_kb."""
    # 0. Explicit override (--kb-dir or OPENKB_DIR)
    if override is not None:
        if (override / ".openkb").is_dir():
            return override
        return None
    # 1. Walk up from cwd
    current = Path.cwd().resolve()
    while True:
        if (current / ".openkb").is_dir():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    # 2. Fall back to global config default_kb
    gc = load_global_config()
    default = gc.get("default_kb")
    if default:
        p = Path(default)
        if (p / ".openkb").is_dir():
            return p
    return None


# Compatibility exports for existing integrations; behavior lives in the application layer.
from openkb.application.generators import (
    preflight_generation as _preflight_skill_new,
    validate_name as _validate_skill_name,
)


def _clear_existing_skill_dir(kb_dir: Path, name: str) -> None:
    """Delete an existing ``<kb>/output/skills/<name>/`` directory."""
    from openkb.skill import skill_dir

    target = skill_dir(kb_dir, name)
    if target.exists():
        shutil.rmtree(target)


def add_single_file(file_path: Path, kb_dir: Path, *, stage: bool = True, bundle=None):
    if bundle is None:
        _setup_llm_key(kb_dir)
    return document_use_cases.add_single_file(
        file_path,
        kb_dir,
        stage=stage,
        bundle=bundle,
        report=click.echo,
    )


def _cleanup_failed_cloud_import(kb_dir: Path, doc_name: str) -> None:
    """Best-effort wiki cleanup after a cloud import whose compilation failed.

    import_cloud_document writes the summary + per-page JSON source before
    compile, and compile_long_doc writes concept/entity pages incrementally — so
    a compile failure (which happens before the registry entry is added) would
    otherwise strand wiki artifacts that ``openkb remove`` cannot reach. Mirror
    remove's wiki cleanup (by doc_name, idempotent) but touch neither the
    registry (no entry was added) nor PageIndex (the cloud doc is the user's).
    """
    from openkb.agent.compiler import (
        remove_doc_from_concept_pages,
        remove_doc_from_entity_pages,
        remove_doc_from_index,
    )

    wiki_dir = kb_dir / "wiki"
    (wiki_dir / "summaries" / f"{doc_name}.md").unlink(missing_ok=True)
    (wiki_dir / "sources" / f"{doc_name}.json").unlink(missing_ok=True)
    images_dir = wiki_dir / "sources" / "images" / doc_name
    if images_dir.is_dir():
        shutil.rmtree(images_dir, ignore_errors=True)
    concept_result = remove_doc_from_concept_pages(wiki_dir, doc_name, keep_empty=False)
    entity_result = remove_doc_from_entity_pages(wiki_dir, doc_name, keep_empty=False)
    remove_doc_from_index(
        wiki_dir,
        doc_name,
        concept_result["deleted"],
        entity_slugs_deleted=entity_result["deleted"],
    )


def import_from_pageindex_cloud(doc_id: str, kb_dir: Path) -> Literal["added", "skipped", "failed"]:
    """Import an existing PageIndex Cloud document into the KB by ``doc_id``.

    Fetches structure + page content from the cloud (no local PDF), compiles
    concepts, and registers a raw-less ``pageindex_cloud`` entry. Idempotent:
    re-importing the same ``doc_id`` is skipped. The user's cloud corpus is
    never modified.
    """
    import hashlib
    from openkb.state import HashRegistry

    logger = logging.getLogger(__name__)
    openkb_dir = kb_dir / ".openkb"
    config = resolve_effective_config(kb_dir)[0]
    _setup_llm_key(kb_dir)
    model: str = config.get("model", DEFAULT_CONFIG["model"])

    path_key = f"pageindex-cloud:{doc_id}"
    synthetic_hash = hashlib.sha256(path_key.encode("utf-8")).hexdigest()

    with kb_ingest_lock(kb_dir / ".openkb"):
        registry = HashRegistry(openkb_dir / "hashes.json")
        if registry.is_known(synthetic_hash):
            click.echo(f"  [SKIP] Already imported from PageIndex Cloud: {doc_id}")
            return "skipped"

    click.echo(f"Importing from PageIndex Cloud: {doc_id}")
    doc_name = ""
    from openkb.add_coordinator import AddMutationPlan, DirtyRollbackError, run_add_mutation

    try:
        try:
            cloud = prepare_cloud_import(doc_id, kb_dir, path_key)
        except Exception as exc:
            click.echo(f"  [ERROR] Import failed: {exc}")
            logger.debug("Cloud import traceback:", exc_info=True)
            return "failed"

        with kb_ingest_lock(kb_dir / ".openkb"):
            registry = HashRegistry(openkb_dir / "hashes.json")
            if registry.is_known(synthetic_hash):
                click.echo(f"  [SKIP] Already imported from PageIndex Cloud: {doc_id}")
                return "skipped"

            stem = _cloud_display_stem(cloud.cloud_name, doc_id)
            doc_name = resolve_doc_name_from_key(stem, path_key, registry)

            def commit_body(_snapshot) -> None:
                summary_path = _write_long_doc_artifacts(
                    cloud.tree,
                    cloud.all_pages,
                    doc_name,
                    doc_id,
                    kb_dir,
                    description=cloud.description,
                )
                _run_compile_with_retry(
                    lambda: compile_long_doc(
                        doc_name,
                        summary_path,
                        doc_id,
                        kb_dir,
                        model,
                        doc_description=cloud.description,
                        max_concurrency=resolve_concurrency(config) or DEFAULT_COMPILE_CONCURRENCY,
                    ),
                    label=f"Compiling imported doc (doc_id={doc_id})",
                )

                # Register the raw-less cloud entry only after successful compilation.
                registry = HashRegistry(openkb_dir / "hashes.json")
                meta = {
                    "name": cloud.cloud_name,
                    "doc_name": doc_name,
                    "type": "pageindex_cloud",
                    "origin": "cloud",
                    "path": path_key,
                    "source_path": _registry_path(
                        kb_dir / "wiki" / "sources" / f"{doc_name}.json", kb_dir
                    ),
                    "doc_id": doc_id,
                }
                registry.remove_by_doc_name(doc_name)
                registry.add(synthetic_hash, meta)

            def append_cloud_log() -> None:
                append_log(kb_dir / "wiki", "ingest", doc_name)

            plan = AddMutationPlan(
                operation="cloud_import",
                details={"doc_id": doc_id, "doc_name": doc_name},
                touched_paths=_snapshot_add_paths(kb_dir, doc_name, None, None),
                body=commit_body,
                post_commit_hooks=[append_cloud_log],
                # Cloud import reads from PageIndex Cloud and writes no local blob,
                # so .openkb/files is never touched — nothing to snapshot there.
                hardlink_dirs={
                    kb_dir / "wiki" / "concepts",
                    kb_dir / "wiki" / "entities",
                },
            )
            if not run_add_mutation(kb_dir, plan):
                return "failed"
    except DirtyRollbackError:
        raise
    except Exception as exc:
        # run_add_mutation handles snapshot/body failures itself (returns False),
        # so this except only catches pre-mutation errors — surface the real cause
        # instead of the old misleading "Failed to prepare mutation snapshot" label.
        click.echo(f"  [ERROR] Cloud import failed for {doc_id}: {exc}")
        logger.debug("Cloud import mutation traceback:", exc_info=True)
        return "failed"

    click.echo(f"  [OK] {doc_name} imported from PageIndex Cloud.")
    return "added"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(package_name="openkb", prog_name="openkb", message="%(prog)s %(version)s")
@click.option("-v", "--verbose", is_flag=True, default=False, help="Enable verbose logging.")
@click.option(
    "--kb-dir",
    "kb_dir_override",
    default=None,
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    help="Path to a KB root directory (overrides auto-detection).",
)
@click.pass_context
def cli(ctx, verbose, kb_dir_override):
    """OpenKB — Karpathy's LLM Knowledge Base workflow, powered by PageIndex."""
    logging.basicConfig(
        format="%(name)s %(levelname)s: %(message)s",
        level=logging.WARNING,
    )
    if verbose:
        logging.getLogger("openkb").setLevel(logging.DEBUG)
    ctx.ensure_object(dict)
    if kb_dir_override:
        ctx.obj["kb_dir_override"] = Path(kb_dir_override)
    else:
        env_kb = os.environ.get("OPENKB_DIR")
        if env_kb:
            ctx.obj["kb_dir_override"] = Path(env_kb).resolve()
        else:
            ctx.obj["kb_dir_override"] = None


def _with_kb_lock(*, exclusive: bool):
    """Wrap a Click command in the appropriate KB lock when a KB exists."""

    def decorator(fn):
        @wraps(fn)
        def wrapper(ctx, *args, **kwargs):
            kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
            if kb_dir is None:
                return fn(ctx, *args, **kwargs)
            if exclusive:
                with kb_ingest_lock(kb_dir / ".openkb"):
                    return fn(ctx, *args, **kwargs)
            with kb_read_lock(kb_dir / ".openkb"):
                return fn(ctx, *args, **kwargs)

        return wrapper

    return decorator


@cli.command()
@click.argument("path", default=".")
def use(path):
    """Set PATH as the default knowledge base."""
    target = Path(path).resolve()
    if not (target / ".openkb").is_dir():
        click.echo(f"Not a knowledge base: {target}")
        return
    register_kb(target)
    click.echo(f"Default KB set to: {target}")


_LANGUAGE_MAX_LEN = 50
_MODEL_MAX_LEN = 100


def _coerce_language(value: str | None) -> str | None:
    """Strip a language string; treat blanks as unset; reject unsafe values.

    The language string is interpolated into LLM system prompts (see
    ``_SYSTEM_TEMPLATE`` in ``openkb/agent/compiler.py`` and the query agent's
    instructions), so values with newlines or excessive length would let an
    external caller smuggle instructions into the prompt. Capping at
    ``_LANGUAGE_MAX_LEN`` and rejecting control characters is enough to close
    that vector while still allowing common forms ("en", "ko", "Korean",
    "Simplified Chinese").

    Returns the cleaned string, or ``None`` if the input was missing or blank
    after stripping. Raises ``click.BadParameter`` on unsafe input.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > _LANGUAGE_MAX_LEN or any(c in value for c in "\n\r\t"):
        raise click.BadParameter(
            f"language must be {_LANGUAGE_MAX_LEN} characters or fewer with no control characters",
            param_hint="'--language'",
        )
    return value


def _language_option_callback(_ctx, _param, value):
    return _coerce_language(value)


def _coerce_model(value: str | None) -> str | None:
    """Strip a model string; treat blanks as unset; reject unsafe values.

    Mirrors ``_coerce_language``. The model string is passed to LiteLLM and
    also echoed in logs/CLI output, so embedded control characters would
    corrupt that output. Capping length keeps pathological values out of
    config.yaml.

    Returns the cleaned string, or ``None`` if the input was missing or blank
    after stripping. Raises ``click.BadParameter`` on unsafe input.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > _MODEL_MAX_LEN or any(c in value for c in "\n\r\t"):
        raise click.BadParameter(
            f"model must be {_MODEL_MAX_LEN} characters or fewer with no control characters",
            param_hint="'--model'",
        )
    return value


def _model_option_callback(_ctx, _param, value):
    return _coerce_model(value)


def _stdin_is_tty() -> bool:
    """Return True when stdin is a real terminal.

    Used to skip optional ``openkb init`` prompts when input is piped or
    redirected, so existing automation (e.g. ``printf '\\n\\n' | openkb init``)
    keeps working as new prompts are added. Mirrors ``_stream_to_tty`` from #45.
    """
    return sys.stdin.isatty()


@cli.command()
@click.option(
    "--model",
    "-m",
    "model",
    default=None,
    metavar="MODEL",
    callback=_model_option_callback,
    help=(
        "LLM in LiteLLM provider/model format "
        "(e.g. 'gpt-5.4', 'anthropic/claude-sonnet-4-6'). "
        "Skips the interactive prompt when set."
    ),
)
@click.option(
    "--language",
    "-l",
    "language",
    default=None,
    metavar="LANG",
    callback=_language_option_callback,
    help="Wiki output language (e.g. 'en', 'ko'). Skips the interactive prompt when set.",
)
def init(model, language):
    """Initialise a new knowledge base in the current directory."""
    openkb_dir = Path(".openkb")
    if openkb_dir.exists():
        click.echo("Knowledge base already initialized.")
        return

    # Interactive prompts
    click.echo("Pick an LLM in `provider/model` LiteLLM format:")
    click.echo("  OpenAI:    gpt-5.4, gpt-5.4-mini")
    click.echo("  Anthropic: anthropic/claude-sonnet-4-6, anthropic/claude-opus-4-6")
    click.echo("  Gemini:    gemini/gemini-3.1-pro-preview, gemini/gemini-3-flash-preview")
    click.echo("  DeepSeek:  deepseek/deepseek-v4-flash, deepseek/deepseek-v4-pro")
    click.echo("  Others:    see https://docs.litellm.ai/docs/providers")
    click.echo()
    if model is None and _stdin_is_tty():
        model = _coerce_model(
            click.prompt(
                f"Model (enter for default {DEFAULT_CONFIG['model']})",
                default=DEFAULT_CONFIG["model"],
                show_default=False,
            )
        )
    if not model:
        model = DEFAULT_CONFIG["model"]
    api_key = click.prompt(
        "LLM API Key (saved to .env, enter to skip)",
        default="",
        hide_input=True,
        show_default=False,
    ).strip()
    if language is None and _stdin_is_tty():
        language = _coerce_language(
            click.prompt(
                f"Wiki language (enter for default {DEFAULT_CONFIG['language']})",
                default=DEFAULT_CONFIG["language"],
                show_default=False,
            )
        )
    if not language:
        language = DEFAULT_CONFIG["language"]
    env_existed = Path(".env").exists()
    initialize_kb(
        Path.cwd(), model=model, language=language, api_key=api_key, seed_environment=False
    )
    if api_key:
        if env_existed:
            click.echo(".env already exists, skipping write. Add LLM_API_KEY manually if needed.")
        else:
            click.echo("Saved LLM API key to .env.")

    click.echo("Knowledge base initialized.")


@cli.command()
@click.argument("path", required=False)
@click.option(
    "--from-pageindex-cloud",
    "from_pageindex_cloud",
    default=None,
    metavar="DOC_ID",
    help="Import an already-indexed PageIndex Cloud document by its doc-id "
    "(no local file). Mutually exclusive with PATH.",
)
@click.pass_context
def add(ctx, path, from_pageindex_cloud):
    """Add a document or directory of documents at PATH to the knowledge base.

    PATH may be a local file, a local directory (which is walked
    recursively for supported extensions), or an http(s) URL. URLs are
    fetched into ``raw/`` first: PDF responses (by Content-Type and
    magic-byte sniff) are saved as ``.pdf``; HTML responses are run
    through trafilatura's main-content extractor and saved as ``.md``.

    Alternatively, pass --from-pageindex-cloud <DOC_ID> to import a document
    that is already indexed in PageIndex Cloud, with no local file. Requires
    the PAGEINDEX_API_KEY environment variable.
    """
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return

    # Cloud import path — mutually exclusive with a local/URL PATH.
    if from_pageindex_cloud is not None:
        if path is not None:
            click.echo("Provide either PATH or --from-pageindex-cloud, not both.")
            return
        outcome = import_from_pageindex_cloud(from_pageindex_cloud, kb_dir)
        if outcome == "failed":
            ctx.exit(1)
        return

    if path is None:
        click.echo("Provide a PATH or use --from-pageindex-cloud <DOC_ID>.")
        return

    from openkb.url_ingest import looks_like_url, fetch_url_to_raw, _unique_path

    if looks_like_url(path):
        from tempfile import TemporaryDirectory

        from openkb.application.uploads import published_input

        # Acquisition owns private bytes outside the KB lease. Publish only a
        # complete input, then retain ownership through compile/skip cleanup.
        with TemporaryDirectory(prefix="openkb-url-") as temporary:
            fetched = fetch_url_to_raw(path, Path(temporary), announce_saved=False)
            if fetched is None:
                return
            with kb_ingest_lock(kb_dir / ".openkb"):
                name = _unique_path(kb_dir / "raw" / fetched.name).name
                with published_input(kb_dir, fetched, filename=name) as published:
                    if published.path.suffix.lower() == ".pdf":
                        size = published.path.stat().st_size / (1024 * 1024)
                        description = f"{size:.1f} MB PDF"
                    else:
                        length = len(published.path.read_text(encoding="utf-8"))
                        description = f"{length // 1024 or 1} KB clean markdown"
                    click.echo(f"  Saved: raw/{published.path.name} ({description})")
                    outcome = add_single_file(published.path, kb_dir)
                    if outcome == "skipped":
                        published.discard_if_unregistered()
            return

    target = Path(path)
    if not target.exists():
        click.echo(f"Path does not exist: {path}")
        return

    if target.is_dir():
        files = [
            f
            for f in sorted(target.rglob("*"))
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        if not files:
            click.echo(f"No supported files found in {path}.")
            return
        total = len(files)
        click.echo(f"Found {total} supported file(s) in {path}.")
        for i, f in enumerate(files, 1):
            click.echo(f"\n[{i}/{total}] ", nl=False)
            add_single_file(f, kb_dir)
    else:
        if target.suffix.lower() not in SUPPORTED_EXTENSIONS:
            click.echo(
                f"Unsupported file type: {target.suffix}. "
                f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )
            return
        add_single_file(target, kb_dir)


def _stream_to_tty() -> bool:
    """Return True when stdout is a real terminal.

    Used to auto-disable streaming output when ``openkb query`` is piped,
    redirected to a file, or run as a subprocess — streaming output emits
    interleaved tool-call lines that are noisy for non-interactive callers,
    and the non-streaming branch returns just the final answer string.
    """
    return sys.stdout.isatty()


from openkb.application.answers import save_exploration as save_exploration


@cli.command()
@click.argument("question")
@click.option("--save", is_flag=True, default=False, help="Save the answer to wiki/explorations/.")
@click.option(
    "--raw",
    "raw",
    is_flag=True,
    default=False,
    help="Show raw markdown source instead of rendered output (keeps tool-call colors).",
)
@click.pass_context
@_with_kb_lock(exclusive=True)
def query(ctx, question, save, raw):
    """Query the knowledge base with QUESTION."""
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return

    from openkb.agent.query import run_query

    config = resolve_effective_config(kb_dir)[0]
    _setup_llm_key(kb_dir)
    model: str = config.get("model", DEFAULT_CONFIG["model"])

    stream = _stream_to_tty()
    try:
        answer = asyncio.run(run_query(question, kb_dir, model, stream=stream, raw=raw))
        if not stream and answer:
            click.echo(answer)
    except Exception as exc:
        click.echo(f"[ERROR] Query failed: {exc}")
        return

    append_log(kb_dir / "wiki", "query", question)

    if save and answer:
        import re
        from openkb.lint import list_existing_wiki_targets, strip_ghost_wikilinks

        slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:60]
        explore_dir = kb_dir / "wiki" / "explorations"
        explore_dir.mkdir(parents=True, exist_ok=True)
        explore_path = explore_dir / f"{slug}.md"
        # Strip ghost wikilinks the agent may have emitted to non-existent
        # concept/summary pages — the schema_md in the agent's instructions
        # encourages [[wikilinks]] but the agent's view of "which pages
        # exist" can drift from disk reality.
        known = list_existing_wiki_targets(kb_dir / "wiki")
        cleaned_answer, _ = strip_ghost_wikilinks(answer, known)
        atomic_write_text(explore_path, f'---\nquery: "{question}"\n---\n\n{cleaned_answer}\n')
        click.echo(f"\nSaved to {explore_path}")


# Transitional private exports for existing CLI integrations. Business lives in application.
from openkb.application.removal import (  # noqa: E402
    _build_remove_plan,
    _execute_remove_plan,
    _resolve_doc_identifier,
    run_remove_for_api as run_remove_for_api,
)


@cli.command()
@click.argument("identifier")
@click.option(
    "--keep-raw", is_flag=True, default=False, help="Don't delete the original file from raw/."
)
@click.option(
    "--keep-empty",
    "--keep-empty-concepts",
    "keep_empty",
    is_flag=True,
    default=False,
    help="Keep concept AND entity pages whose only source was the "
    "removed doc (leaving an empty sources: [] list). Useful "
    "when replacing the doc with a newer version. "
    "(--keep-empty-concepts is a backward-compatible alias.)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would be done without modifying anything.",
)
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip the confirmation prompt.")
@click.pass_context
@_with_kb_lock(exclusive=True)
def remove(ctx, identifier, keep_raw, keep_empty, dry_run, yes):
    """Remove a document from the knowledge base.

    IDENTIFIER may be the original filename ("paper.pdf"), the doc_name
    slug ("paper-a1b2c3d4e5f6"), or a substring that uniquely matches one.

    Deletes the doc's summary and source files, prunes the doc from
    concept- and entity-page frontmatter and Related Documents sections,
    drops the Documents entry from index.md, removes the hash entry, and
    finally runs `lint --fix` to clean any dangling wikilinks.

    Concept and entity pages whose only source was this doc are deleted by
    default; use --keep-empty to retain them.
    """
    from openkb.state import HashRegistry

    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return

    openkb_dir = kb_dir / ".openkb"
    registry = HashRegistry(openkb_dir / "hashes.json")

    matches = _resolve_doc_identifier(registry, identifier)
    if not matches:
        click.echo(f"No document matching '{identifier}' found in the KB.")
        click.echo("Try `openkb list` to see indexed documents.")
        return
    if len(matches) > 1:
        click.echo(f"'{identifier}' matches multiple documents:")
        for _, m in matches:
            click.echo(f"  - {m.get('name', '?')}  (doc_name: {m.get('doc_name', '?')})")
        click.echo("Use a more specific name or the exact doc_name slug.")
        return

    file_hash, meta = matches[0]
    plan = _build_remove_plan(
        kb_dir,
        file_hash,
        meta,
        keep_raw=keep_raw,
        keep_empty=keep_empty,
    )

    # ----- Print the plan -----
    click.echo(f"Removing '{plan.name}' (doc_name: {plan.doc_name}, type: {plan.doc_type or '?'}).")
    click.echo("")
    for action in plan.actions:
        click.echo(f"  {action.tag:<8} {action.target}")
    if plan.concept_deletes:
        click.echo("")
        click.echo(
            f"  {len(plan.concept_deletes)} concept(s) will be DELETED because this is their only source."
        )
        click.echo("  Pass --keep-empty to retain them instead.")
    if plan.entity_deletes:
        click.echo("")
        click.echo(
            f"  {len(plan.entity_deletes)} entity(s) will be DELETED because this is their only source."
        )
        click.echo("  Pass --keep-empty to retain them instead.")
    click.echo("")

    if dry_run:
        click.echo("(dry-run — nothing modified)")
        return

    if not yes:
        if not click.confirm("Proceed?", default=False):
            click.echo("Aborted.")
            return

    result = _execute_remove_plan(kb_dir, plan, registry, keep_empty=keep_empty)

    if result.lint_files_changed:
        click.echo(
            f"  lint --fix cleaned {result.lint_ghosts_removed} dangling wikilink(s) in {result.lint_files_changed} file(s)"
        )
    if result.pageindex_message is not None:
        click.echo(f"  PageIndex: {result.pageindex_message}")
    if result.pageindex_error is not None:
        click.echo(
            f"  [WARN] PageIndex cleanup failed: {result.pageindex_error} "
            f"— registry entry kept; re-run `openkb remove {result.name}` to retry"
        )
        return

    click.echo(f"  [OK] {result.name} removed from knowledge base.")


def _refresh_schema(wiki_dir: Path) -> bool:
    from openkb.application.recompilation import refresh_schema

    changed = refresh_schema(wiki_dir.parent)
    if changed:
        click.echo("  Backed up existing schema to wiki/AGENTS.md.bak")
        click.echo("  Refreshed wiki/AGENTS.md to the current schema.")
    return changed


@cli.command(name="delete-kb")
@click.argument("name")
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="Skip the type-the-name confirmation."
)
def delete_kb_cmd(name, yes):
    """Permanently delete a knowledge base (physical removal, irreversible).

    NAME is the KB name as addressed by the web UI / registry. Removes the
    entire KB directory (raw docs + wiki) and unregisters it from the global
    config. There is no undo.
    """
    from openkb.config import _is_kb_dir, registered_kbs, resolve_kb_alias
    from openkb.kb_admin import delete_kb

    try:
        kb_dir = resolve_kb_alias(name)
    except ValueError as exc:
        click.echo(f"Invalid KB name: {exc}")
        return
    # Accept a live KB dir OR a registered ghost (directory already removed by
    # hand) so the stuck registry entry has a cleanup path; reject anything else.
    registered = any(p == kb_dir for _, p in registered_kbs())
    if not _is_kb_dir(kb_dir) and not registered:
        click.echo(f"No knowledge base named '{name}' found.")
        return
    click.echo(f"About to PERMANENTLY delete knowledge base '{name}':")
    click.echo(f"  {kb_dir}")
    click.echo("This removes the entire directory (raw docs + wiki) and cannot be undone.")
    if not yes:
        typed = click.prompt("Type the KB name to confirm", default="", show_default=False)
        if typed.strip() != name:
            click.echo("Name did not match — aborted.")
            return
    delete_kb(kb_dir)
    click.echo(f"Deleted knowledge base '{name}'.")


@cli.command()
@click.argument("doc_name", required=False)
@click.option(
    "--all", "all_docs", is_flag=True, default=False, help="Recompile every indexed document."
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="List the docs that would be recompiled; no LLM calls, no writes.",
)
@click.option(
    "--yes", "-y", is_flag=True, default=False, help="Skip the --all confirmation prompt."
)
@click.option(
    "--refresh-schema",
    "refresh_schema",
    is_flag=True,
    default=False,
    help="Overwrite wiki/AGENTS.md with the bundled schema (backs up "
    "the old one to AGENTS.md.bak) if it differs.",
)
@click.pass_context
def recompile(ctx, doc_name, all_docs, dry_run, yes, refresh_schema):
    """Re-run the current compile pipeline on already-indexed documents.

    Recompiling re-runs the same ``compile_short_doc`` / ``compile_long_doc``
    that ``openkb add`` uses, so pre-feature KBs gain the ``entities/`` layer
    and pages refresh to the current format. It does NOT re-run PageIndex or
    re-convert raw files — it reuses the on-disk ``wiki/sources/`` and
    ``wiki/summaries/`` content (and the registry's PageIndex ``doc_id``).

    DOC_NAME recompiles one doc (resolved like ``openkb remove`` — filename,
    slug, or unique substring). ``--all`` recompiles every indexed doc.
    Exactly one of DOC_NAME or ``--all`` is required.

    Side effect: this regenerates summaries (short docs) and rewrites concept
    pages with the current logic — manual edits to those pages are overwritten.
    """
    from openkb.application.recompilation import recompile_document, select_recompilation

    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return
    selection = select_recompilation(kb_dir, doc_name, all_docs=all_docs)
    targets = selection.targets
    if selection.status != "ready":
        if selection.status == "invalid":
            click.echo(
                "Specify either a DOC_NAME or --all, not both."
                if all_docs
                else "Specify a document name or pass --all to recompile every doc."
            )
        elif selection.status == "empty":
            click.echo("No documents indexed yet. Run `openkb add` first.")
        elif selection.status == "not_found":
            click.echo(f"No document matching '{doc_name}' found in the KB.")
            click.echo("Try `openkb list` to see indexed documents.")
        else:
            click.echo(f"'{doc_name}' matches multiple documents:")
            for target in targets:
                click.echo(f"  - {target.name}  (doc_name: {target.doc_name})")
            click.echo("Use a more specific name or the exact doc_name slug.")
        return
    if dry_run:
        click.echo(f"Would recompile {len(targets)} document(s):")
        for target in targets:
            click.echo(f"  - {target.doc_name}  ({target.kind})")
        click.echo(
            "\nNote: recompiling regenerates summaries (short docs) and rewrites "
            "concept pages — manual edits would be overwritten."
        )
        click.echo("(dry-run — nothing modified)")
        return
    if all_docs and not yes:
        click.echo(
            f"This will recompile {len(targets)} document(s), regenerating "
            "summaries and rewriting concept pages with the current logic.\n"
            "Manual edits to those pages will be overwritten."
        )
        if not click.confirm("Proceed?", default=False):
            click.echo("Aborted.")
            return
    if refresh_schema:
        _refresh_schema(kb_dir / "wiki")
    _setup_llm_key(kb_dir)
    config = resolve_effective_config(kb_dir)[0]
    model = config.get("model", DEFAULT_CONFIG["model"])
    concurrency = resolve_concurrency(config) or DEFAULT_COMPILE_CONCURRENCY
    recompiled = skipped = 0
    for i, target in enumerate(targets, 1):
        click.echo(f"[{i}/{len(targets)}] Recompiling {target.kind} doc {target.doc_name}...")
        result = asyncio.run(
            recompile_document(kb_dir, target.file_hash, model=model, max_concurrency=concurrency)
        )
        if result.status == "compiled":
            recompiled += 1
            click.echo(f"  [OK] {result.name} ({result.elapsed:.1f}s)")
        else:
            skipped += 1
            label = "ERROR" if result.status == "failed" else "SKIP"
            detail = f" ({result.error_type})" if result.error_type else ""
            click.echo(f"  [{label}] {result.name}: {result.message}{detail}")
    click.echo(f"\nDone: recompiled {recompiled}, skipped {skipped}.")
    append_log(kb_dir / "wiki", "recompile", f"recompiled {recompiled}, skipped {skipped}")


# Temporary import compatibility for callers migrating to the REST adapter.
from openkb.api_recompile import iter_recompile as iter_recompile


@cli.command()
@click.option(
    "--resume",
    "-r",
    "resume",
    is_flag=False,
    flag_value="__latest__",
    default=None,
    metavar="[ID]",
    help="Resume the latest chat session, or a specific one by id or prefix.",
)
@click.option(
    "--list",
    "list_sessions_flag",
    is_flag=True,
    default=False,
    help="List chat sessions.",
)
@click.option(
    "--delete",
    "delete_id",
    default=None,
    metavar="ID",
    help="Delete a chat session by id or prefix.",
)
@click.option(
    "--no-color",
    "no_color",
    is_flag=True,
    default=False,
    help="Disable colored output.",
)
@click.option(
    "--raw",
    "raw",
    is_flag=True,
    default=False,
    help="Show raw markdown source instead of rendered output (keeps prompt and tool-call colors).",
)
@click.pass_context
def chat(ctx, resume, list_sessions_flag, delete_id, no_color, raw):
    """Start an interactive chat with the knowledge base."""
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return

    from openkb.agent.chat_session import (
        ChatSession,
        delete_session,
        list_sessions,
        load_session,
        relative_time,
        resolve_session_id,
    )

    if list_sessions_flag:
        sessions = list_sessions(kb_dir)
        if not sessions:
            click.echo("No chat sessions yet.")
            return
        click.echo(f"  {'ID':<22} {'TURNS':<6} {'UPDATED':<12} TITLE")
        click.echo(f"  {'-' * 22} {'-' * 6} {'-' * 12} {'-' * 30}")
        for s in sessions:
            rel = relative_time(s.get("updated_at", ""))
            title = s.get("title") or "(empty)"
            click.echo(f"  {s['id']:<22} {s['turn_count']:<6} {rel:<12} {title}")
        click.echo(f"\n{len(sessions)} session(s) in {kb_dir / '.openkb' / 'chats'}")
        return

    if delete_id is not None:
        try:
            resolved = resolve_session_id(kb_dir, delete_id)
        except ValueError as exc:
            click.echo(f"[ERROR] {exc}")
            return
        if not resolved:
            click.echo(f"No matching session: {delete_id}")
            return
        if delete_session(kb_dir, resolved):
            click.echo(f"Deleted session {resolved}")
        else:
            click.echo(f"Could not delete session: {resolved}")
        return

    config = resolve_effective_config(kb_dir)[0]
    _setup_llm_key(kb_dir)

    if resume is not None:
        try:
            resolved = resolve_session_id(kb_dir, resume)
        except ValueError as exc:
            click.echo(f"[ERROR] {exc}")
            return
        if not resolved:
            if resume == "__latest__":
                click.echo("No previous chat sessions to resume.")
            else:
                click.echo(f"No matching session: {resume}")
            return
        session = load_session(kb_dir, resolved)
    else:
        model: str = config.get("model", DEFAULT_CONFIG["model"])
        language: str = config.get("language", "en")
        session = ChatSession.new(kb_dir, model, language)

    from openkb.agent.chat import run_chat

    try:
        asyncio.run(run_chat(kb_dir, session, no_color=no_color, raw=raw))
    except Exception as exc:
        click.echo(f"[ERROR] Chat failed: {exc}")


@cli.command()
@click.pass_context
def watch(ctx):
    """Watch the raw/ directory for new documents and process them automatically."""
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return

    from openkb.watcher import watch_directory

    raw_dir = kb_dir / "raw"
    raw_dir.mkdir(exist_ok=True)

    def on_new_files(paths):
        for p in paths:
            fp = Path(p)
            if fp.suffix.lower() not in SUPPORTED_EXTENSIONS:
                click.echo(
                    f"Skipping unsupported file type: {fp.suffix}. "
                    f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
                )
                continue
            add_single_file(fp, kb_dir)

    click.echo(f"Watching {raw_dir} for new documents. Press Ctrl+C to stop.")
    watch_directory(raw_dir, on_new_files)


async def run_lint(kb_dir: Path, *, fix: bool = False) -> Path | None:
    """CLI/chat projection; shared maintenance owns complete checks and commits."""
    from openkb.api_lint import echo_lint_event, fix_summary
    from openkb.application.maintenance import LintOptions, check_knowledge

    def event(value):
        if value.get("stage") == "links_repaired":
            click.echo(fix_summary(value["files"], value["ghosts"]))
        else:
            echo_lint_event(value)

    result = await check_knowledge(
        kb_dir,
        LintOptions(fix=fix, unique_report=False),
        on_event=event,
        prepare_model=lambda: _setup_llm_key(kb_dir),
    )
    if result.status == "skipped":
        click.echo("Nothing to lint — no documents indexed yet. Run `openkb add` first.")
        return None
    if result.status != "completed" or result.report_path is None:
        raise RuntimeError(f"Lint failed ({result.error_type or result.status})")
    click.echo(f"\nReport written to {result.report_path}")
    return Path(result.report_path)


@cli.command()
@click.option(
    "--fix",
    is_flag=True,
    default=False,
    help="Rewrite broken [[wikilinks]] in place (fuzzy match) or "
    "strip to plain text when no match. Runs before the report.",
)
@click.pass_context
def lint(ctx, fix):
    """Lint the knowledge base for structural and semantic inconsistencies."""
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return
    asyncio.run(run_lint(kb_dir, fix=fix))


@cli.command()
@click.option(
    "--open/--no-open",
    "open_browser",
    default=True,
    help="Open the graph in your browser after generating (default: on; --no-open for headless).",
)
@click.pass_context
def visualize(ctx, open_browser):
    """Render the wiki's [[wikilink]] graph as a self-contained interactive HTML page."""
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return
    from openkb.application.artifacts import generate_graph

    result = generate_graph(kb_dir)
    graph, out = result.graph, result.path
    if out is None:
        click.echo("No wiki pages to visualize yet. Run `openkb add` first.")
        return
    click.echo(
        f"Graph written to {out}  ({len(graph['nodes'])} nodes, {len(graph['edges'])} edges)"
    )
    if open_browser:
        import webbrowser

        try:
            opened = webbrowser.open(
                out.resolve().as_uri()
            )  # resolve() so a relative --kb-dir still yields a valid file URI
        except Exception:
            opened = False
        if not opened:
            click.echo(
                "(couldn't launch a browser — open the file above manually, or use --no-open)"
            )


def print_list(kb_dir: Path) -> None:
    """Print all documents in the knowledge base. Usable from CLI and chat REPL."""
    openkb_dir = kb_dir / ".openkb"
    hashes_file = openkb_dir / "hashes.json"
    if not hashes_file.exists():
        click.echo("No documents indexed yet.")
        return

    hashes = json.loads(hashes_file.read_text(encoding="utf-8"))
    if not hashes:
        click.echo("No documents indexed yet.")
        return

    # Display documents table with count in header
    doc_count = len(hashes)
    click.echo(f"Documents ({doc_count}):")
    click.echo(f"  {'Name':<40} {'Type':<12} {'Pages':<8}")
    click.echo(f"  {'-' * 40} {'-' * 12} {'-' * 8}")
    for file_hash, meta in hashes.items():
        name = meta.get("name", "unknown")
        raw_type = meta.get("type", "unknown")
        display = _display_type(raw_type)
        pages = meta.get("pages", "")
        pages_str = str(pages) if pages else ""
        click.echo(f"  {name:<40} {display:<12} {pages_str:<8}")

    # Display summaries
    summaries_dir = kb_dir / "wiki" / "summaries"
    if summaries_dir.exists():
        summaries = sorted(p.stem for p in summaries_dir.glob("*.md"))
        if summaries:
            click.echo(f"\nSummaries ({len(summaries)}):")
            for s in summaries:
                click.echo(f"  - {s}")

    # Display concepts
    concepts_dir = kb_dir / "wiki" / "concepts"
    if concepts_dir.exists():
        concepts = sorted(p.stem for p in concepts_dir.glob("*.md"))
        if concepts:
            click.echo(f"\nConcepts ({len(concepts)}):")
            for c in concepts:
                click.echo(f"  - {c}")

    # Display entities
    entities_dir = kb_dir / "wiki" / "entities"
    if entities_dir.exists():
        entities = sorted(p.stem for p in entities_dir.glob("*.md"))
        if entities:
            click.echo(f"\nEntities ({len(entities)}):")
            for e in entities:
                click.echo(f"  - {e}")

    # Display reports
    reports_dir = kb_dir / "wiki" / "reports"
    if reports_dir.exists():
        reports = sorted(p.name for p in reports_dir.glob("*.md"))
        if reports:
            click.echo(f"\nReports ({len(reports)}):")
            for r in reports:
                click.echo(f"  - {r}")


@cli.command(name="list")
@click.pass_context
@_with_kb_lock(exclusive=False)
def list_cmd(ctx):
    """List all documents in the knowledge base."""
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return
    print_list(kb_dir)


def print_status(kb_dir: Path) -> None:
    """Print knowledge base status. Usable from CLI and chat REPL."""
    wiki_dir = kb_dir / "wiki"
    subdirs = ["sources", "summaries", "concepts", "entities", "reports"]

    # Print the active KB path as the first line. Agents and scripts
    # parse this to locate the wiki without assuming cwd == KB root.
    click.echo(f"Knowledge base: {kb_dir}")
    click.echo("")
    click.echo("Knowledge Base Status:")
    click.echo(f"  {'Directory':<20} {'Files':<10}")
    click.echo(f"  {'-' * 20} {'-' * 10}")

    for subdir in subdirs:
        path = wiki_dir / subdir
        if path.exists():
            count = len(list(path.glob("*.md")))
        else:
            count = 0
        click.echo(f"  {subdir:<20} {count:<10}")

    # Raw files
    raw_dir = kb_dir / "raw"
    if raw_dir.exists():
        raw_count = len([f for f in raw_dir.iterdir() if f.is_file()])
        click.echo(f"  {'raw':<20} {raw_count:<10}")

    # Hash registry summary
    openkb_dir = kb_dir / ".openkb"
    hashes_file = openkb_dir / "hashes.json"
    if hashes_file.exists():
        hashes = json.loads(hashes_file.read_text(encoding="utf-8"))
        click.echo(f"\n  Total indexed: {len(hashes)} document(s)")

    # Last compile time: newest compiled page across summaries/, concepts/,
    # and entities/ (an entity-only compile must still bump the shown time).
    compiled_pages = [
        p
        for sub in PAGE_CONTENT_DIRS
        for p in (wiki_dir / sub).glob("*.md")
        if (wiki_dir / sub).exists()
    ]
    if compiled_pages:
        newest_page = max(compiled_pages, key=lambda p: p.stat().st_mtime)
        import datetime

        mtime = datetime.datetime.fromtimestamp(newest_page.stat().st_mtime)
        click.echo(f"  Last compile:  {mtime.strftime('%Y-%m-%d %H:%M:%S')}")

    # Last lint time: newest file in wiki/reports/
    reports_dir = wiki_dir / "reports"
    if reports_dir.exists():
        reports = list(reports_dir.glob("*.md"))
        if reports:
            newest_report = max(reports, key=lambda p: p.stat().st_mtime)
            import datetime

            mtime = datetime.datetime.fromtimestamp(newest_report.stat().st_mtime)
            click.echo(f"  Last lint:     {mtime.strftime('%Y-%m-%d %H:%M:%S')}")


@cli.command()
@click.pass_context
@_with_kb_lock(exclusive=False)
def status(ctx):
    """Show the current status of the knowledge base.

    Output starts with a ``Knowledge base: <path>`` line so agents and
    scripts can locate the wiki without assuming cwd == KB root.
    """
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.")
        return
    print_status(kb_dir)


# ---------------------------------------------------------------------------
# feedback
# ---------------------------------------------------------------------------

_FEEDBACK_REPO = "VectifyAI/OpenKB"
_FEEDBACK_TYPES = ("bug", "feature", "question", "other")
_FEEDBACK_LABEL_MAP = {
    "bug": "bug",
    "feature": "enhancement",
    "question": "question",
    "other": "",
}


def _openkb_version() -> str:
    """Return the installed openkb package version.

    Delegates to ``openkb.__version__`` so the chat REPL, feedback issue
    body, and any future caller all surface the same fallback string
    (``0.0.0+unknown`` from ``openkb/__init__.py``). Mirrors
    ``openkb.agent.chat._openkb_version``.
    """
    from openkb import __version__

    return __version__


def _collect_feedback_diagnostics(ctx) -> dict[str, str]:
    """Auto-collect non-sensitive environment info to attach to a feedback
    issue. Kept deliberately small — no paths, no API keys, no usernames.
    """
    import platform

    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override") if ctx.obj else None)
    return {
        "openkb": _openkb_version(),
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()}",
        "kb_initialised": "yes" if kb_dir else "no",
    }


def _build_feedback_url(
    message: str,
    feedback_type: str,
    diagnostics: dict[str, str],
) -> str:
    """Build a GitHub issue URL with title / body / labels prefilled."""
    from urllib.parse import urlencode

    first_line = message.splitlines()[0] if message else ""
    truncated = first_line[:60] + ("…" if len(first_line) > 60 else "")
    title_prefix = f"[{feedback_type}] " if feedback_type != "other" else ""
    title = f"{title_prefix}{truncated}" if truncated else f"{title_prefix}Feedback from CLI"

    if diagnostics:
        diag_block = "\n".join(f"- **{k}**: {v}" for k, v in diagnostics.items())
        body = (
            f"{message}\n\n"
            "---\n\n"
            "<details>\n"
            "<summary>Diagnostics (auto-collected by <code>openkb feedback</code>)</summary>\n\n"
            f"{diag_block}\n"
            "</details>\n"
        )
    else:
        body = message

    params = {"title": title, "body": body}
    label = _FEEDBACK_LABEL_MAP.get(feedback_type, "")
    if label:
        params["labels"] = label

    return f"https://github.com/{_FEEDBACK_REPO}/issues/new?{urlencode(params)}"


@cli.command()
@click.argument("message", required=False)
@click.option(
    "--type",
    "feedback_type",
    type=click.Choice(_FEEDBACK_TYPES),
    default=None,
    help="Feedback type — sets the GitHub issue label.",
)
@click.pass_context
def feedback(ctx, message, feedback_type):
    """Submit feedback by opening a prefilled GitHub issue.

    Examples:

      \b
      openkb feedback                              # interactive
      openkb feedback "openkb add hangs on .docx"  # one-line bug report
      openkb feedback --type feature "..."         # tags the issue 'enhancement'

    The command does not send anything to OpenKB maintainers directly —
    it opens GitHub in your browser with title, body, and label prefilled.
    You log in with your own GitHub account and submit the issue.
    """
    if not message:
        click.echo(
            "What's your feedback? End with an empty line + Ctrl-D "
            "(Unix) or Ctrl-Z+Enter (Windows). Ctrl-C cancels."
        )
        message = sys.stdin.read().strip()

    if not message:
        click.echo("No feedback provided. Aborted.")
        ctx.exit(1)
        return

    if feedback_type is None:
        # Skip the prompt in non-TTY contexts (CI / piped stdin) so
        # ``echo "msg" | openkb feedback`` doesn't hang on the second
        # prompt after consuming all piped input for the message body.
        # Mirrors the ``_stdin_is_tty()`` gate added in PR #48.
        if _stdin_is_tty():
            feedback_type = click.prompt(
                "Type",
                default="other",
                type=click.Choice(_FEEDBACK_TYPES),
                show_default=True,
                show_choices=True,
            )
        else:
            feedback_type = "other"

    diagnostics = _collect_feedback_diagnostics(ctx)
    url = _build_feedback_url(message, feedback_type, diagnostics)

    click.echo("Copy this URL into a browser if the auto-open below fails:")
    click.echo(f"  {url}")

    import webbrowser

    try:
        opened = webbrowser.open(url)
    except Exception as exc:
        # webbrowser.open rarely raises but be defensive — the printed URL
        # above is the fallback path.
        click.echo(f"  (browser auto-open failed: {exc})", err=True)
        return

    # ``webbrowser.open`` returns False on headless boxes (no GUI, no
    # ``BROWSER`` env) without raising. Without this check we'd silently
    # print "Opened" and the user would think the issue was filed.
    if opened:
        click.echo("Opened GitHub in your browser.")
    else:
        click.echo(
            "  (no browser available — copy the URL above to file the issue)",
            err=True,
        )


# ---------------------------------------------------------------------------
# `openkb skill ...` — skill factory (v0.1)
# ---------------------------------------------------------------------------


@cli.group()
def skill():
    """Compile knowledge into a redistributable Anthropic Skill."""


@skill.command("new")
@click.argument("name")
@click.argument("intent")
@click.option(
    "-y",
    "--yes",
    "yes_flag",
    is_flag=True,
    default=False,
    help="Overwrite existing output/skills/<name>/ without prompting.",
)
@click.pass_context
def skill_new(ctx, name, intent, yes_flag):
    """Compile a new skill from this KB's wiki.

    NAME is a kebab-case slug used for the output directory and skill name.
    INTENT is a natural-language description of what this skill should do.

    Example:

      openkb skill new karpathy-thinking "Reason about transformers like Karpathy"
    """
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.", err=True)
        ctx.exit(1)

    err = _preflight_skill_new(kb_dir, name)
    if err:
        click.echo(f"[ERROR] {err}", err=True)
        ctx.exit(1)

    # Verify LLM key + load config BEFORE touching existing output. Any
    # failure here (missing API key, malformed config) must leave the old
    # skill directory intact — we can't replace it if we can't proceed.
    try:
        _setup_llm_key(kb_dir)
    except RuntimeError as exc:
        click.echo(f"[ERROR] {exc}", err=True)
        ctx.exit(1)
    config = resolve_effective_config(kb_dir)[0]
    model = config.get("model", DEFAULT_CONFIG["model"])

    from openkb.application.generators import (
        GenerationOptions,
        generate_artifact,
        preview_generation,
    )

    preview = preview_generation(kb_dir, "skill", name)
    if preview.exists and not yes_flag:
        if not sys.stdin.isatty():
            click.echo(
                f"[ERROR] output/skills/{name}/ exists. Pass -y to overwrite in non-interactive contexts.",
                err=True,
            )
            ctx.exit(1)
        if not click.confirm(f"output/skills/{name}/ already exists. Overwrite?", default=False):
            click.echo("Aborted.")
            ctx.exit(1)
    click.echo(f"Compiling skill '{name}'...")
    gen = asyncio.run(
        generate_artifact(
            kb_dir,
            GenerationOptions("skill", name, intent, overwrite="archive", version=preview.version),
            model=model,
        )
    )
    if gen.status != "completed":
        click.echo(f"[ERROR] {gen.message}", err=True)
        ctx.exit(1)
    saved_iteration = gen.archive_path

    # Surface validation issues. Don't block — files are on disk and
    # the user can fix or rollback.
    result = gen.validation
    if result is not None and (result.errors or result.warnings):
        click.echo("\n[WARN] Validation found issues:")
        for err in result.errors:
            click.echo(f"  ERROR:   {err}")
        for warn in result.warnings:
            click.echo(f"  WARN:    {warn}")
        click.echo(
            f"\nRun `openkb skill validate {name}` to re-check, or "
            f"`openkb skill rollback {name}` to revert."
        )

    click.echo(f"\nSaved: output/skills/{name}/")
    if saved_iteration is not None:
        rel = saved_iteration.relative_to(kb_dir)
        click.echo(f"Previous version: {rel}/  (run `openkb skill rollback {name}` to restore)")
    click.echo("Manifest: .claude-plugin/marketplace.json updated")
    click.echo("\nInstall locally:")
    click.echo(f"  cp -r output/skills/{name} ~/.claude/skills/")
    click.echo("\nShare (push KB to GitHub, then):")
    click.echo("  npx skills@latest add <owner>/<repo>")


@skill.command("history")
@click.argument("name")
@click.pass_context
def skill_history(ctx, name):
    """List previous iterations of a skill."""
    import datetime as _dt

    from openkb.skill.workspace import list_iterations

    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.", err=True)
        ctx.exit(1)

    err = _validate_skill_name(name)
    if err:
        click.echo(f"[ERROR] {err}", err=True)
        ctx.exit(1)

    iters = list_iterations(kb_dir, name)
    if not iters:
        click.echo(f"No previous iterations for '{name}'.")
        return

    click.echo(f"Iterations of '{name}' ({len(iters)} total):\n")
    click.echo("  N  Path                                                  Created")
    click.echo("  -  --------------------------------------------------    -------")
    for path in iters:
        n = int(path.name.split("-", 1)[1])
        rel = path.relative_to(kb_dir)
        try:
            mtime = _dt.datetime.fromtimestamp(path.stat().st_mtime)
            stamp = mtime.strftime("%Y-%m-%d %H:%M")
        except OSError:
            stamp = "-"
        click.echo(f"  {n}  {rel}  {stamp}")

    from openkb.skill import skill_dir

    current = skill_dir(kb_dir, name)
    if current.is_dir():
        rel_curr = current.relative_to(kb_dir)
        click.echo(f"\n  Current: {rel_curr}/")

    latest_n = int(iters[-1].name.split("-", 1)[1])
    click.echo("\nRestore an iteration:")
    click.echo(f"  openkb skill rollback {name}          # restore latest (iteration-{latest_n})")
    click.echo(f"  openkb skill rollback {name} --to 1   # restore iteration-1")


@skill.command("rollback")
@click.argument("name")
@click.option(
    "--to",
    "to_n",
    default=None,
    type=int,
    help="Iteration number to restore. Defaults to latest.",
)
@click.option(
    "-y",
    "--yes",
    "yes_flag",
    is_flag=True,
    default=False,
    help="Skip confirmation.",
)
@click.pass_context
@_with_kb_lock(exclusive=True)
def skill_rollback(ctx, name, to_n, yes_flag):
    """Restore a previous iteration as the current skill."""
    from openkb.application.skill_maintenance import rollback_skill
    from openkb.skill.workspace import list_iterations

    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.", err=True)
        ctx.exit(1)

    err = _validate_skill_name(name)
    if err:
        click.echo(f"[ERROR] {err}", err=True)
        ctx.exit(1)

    iters = list_iterations(kb_dir, name)
    if not iters:
        click.echo(
            f"[ERROR] No iterations exist for '{name}'. Nothing to roll back.",
            err=True,
        )
        ctx.exit(1)

    target_n = to_n if to_n is not None else int(iters[-1].name.split("-", 1)[1])
    target_label = f"iteration-{target_n}"
    if not any(p.name == target_label for p in iters):
        click.echo(
            f"[ERROR] Iteration {target_n} not found for '{name}'. "
            f"Run `openkb skill history {name}` to see available iterations.",
            err=True,
        )
        ctx.exit(1)

    from openkb.skill import skill_dir

    current = skill_dir(kb_dir, name)
    if current.exists():
        prompt = f"This will overwrite output/skills/{name}/ with {target_label}. Continue?"
        if yes_flag:
            pass
        elif sys.stdin.isatty():
            if not click.confirm(prompt, default=False):
                click.echo("Aborted.")
                ctx.exit(1)
        else:
            click.echo(
                f"[ERROR] output/skills/{name}/ exists. Pass -y to overwrite "
                f"in non-interactive contexts.",
                err=True,
            )
            ctx.exit(1)

    try:
        rollback_skill(kb_dir, name, iteration=target_n)
    except FileNotFoundError as exc:
        click.echo(f"[ERROR] {exc}", err=True)
        ctx.exit(1)

    click.echo(f"Restored output/skills/{name}/ from {target_label}.")
    click.echo("Manifest: .claude-plugin/marketplace.json updated")


@skill.command("validate")
@click.argument("name", required=False)
@click.option(
    "--strict",
    is_flag=True,
    default=False,
    help="Treat warnings as failures (exit non-zero).",
)
@click.pass_context
def skill_validate(ctx, name, strict):
    """Validate one skill (by name) or all compiled skills in this KB."""
    from openkb.skill import skill_dir, skills_root
    from openkb.skill.validator import validate_skill

    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.", err=True)
        ctx.exit(1)

    root = skills_root(kb_dir)
    if not root.is_dir():
        click.echo("No skills found. Compile one with `openkb skill new`.")
        return

    if name:
        target = skill_dir(kb_dir, name)
        if not target.is_dir():
            click.echo(f"[ERROR] Skill '{name}' not found.", err=True)
            ctx.exit(1)
        targets = [target]
    else:
        targets = sorted(
            d for d in root.iterdir() if d.is_dir() and not d.name.endswith("-workspace")
        )

    any_failed = False
    for t in targets:
        result = validate_skill(t, strict=strict)
        passed = result.passed_strict if strict else result.passed
        prefix = "[OK]" if passed else "[FAIL]"
        click.echo(f"{prefix} {t.name}")
        for err in result.errors:
            click.echo(f"  ERROR:   {err}")
        for warn in result.warnings:
            click.echo(f"  WARN:    {warn}")
        if not passed:
            any_failed = True

    if any_failed:
        ctx.exit(1)


@skill.command("eval")
@click.argument("name")
@click.option(
    "--save",
    "save_flag",
    is_flag=True,
    default=False,
    help="Persist the generated eval set to .openkb/eval-sets/<name>.json",
)
@click.option(
    "--eval-set",
    "eval_set_path",
    default=None,
    type=click.Path(),
    help="Use a saved eval set instead of generating fresh prompts.",
)
@click.option(
    "--count",
    default=10,
    type=int,
    help="Number of should-trigger + should-not prompts (each).",
)
@click.pass_context
@_with_kb_lock(exclusive=True)
def skill_eval(ctx, name, save_flag, eval_set_path, count):
    """Measure how accurately a compiled skill's description fires.

    Generates trigger-eval prompts via LLM, then asks a grader LLM whether
    the description should activate the skill for each prompt. Prints pass
    rate + miss list.
    """
    from openkb.skill.evaluator import (
        run_eval,
        save_eval_set,
        load_eval_set,
        EvalPrompt,
    )

    from openkb.skill import skill_dir as _skill_dir

    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.", err=True)
        ctx.exit(1)

    skill_dir = _skill_dir(kb_dir, name)
    if not skill_dir.is_dir():
        click.echo(f"[ERROR] Skill '{name}' not found.", err=True)
        ctx.exit(1)

    try:
        _setup_llm_key(kb_dir)
    except RuntimeError as exc:
        click.echo(f"[ERROR] {exc}", err=True)
        ctx.exit(1)
    config = resolve_effective_config(kb_dir)[0]
    model = config.get("model", DEFAULT_CONFIG["model"])

    eval_set: list[EvalPrompt] | None = None
    if eval_set_path:
        eval_set = load_eval_set(Path(eval_set_path))
        click.echo(f"Loaded eval set from {eval_set_path} ({len(eval_set)} prompts).")
    else:
        click.echo(f"Generating eval set for '{name}' (count={count} per side)...")

    try:
        result = asyncio.run(
            run_eval(
                skill_dir,
                model=model,
                eval_set=eval_set,
                count=count,
            )
        )
    except RuntimeError as exc:
        click.echo(f"[ERROR] {exc}", err=True)
        ctx.exit(1)

    click.echo(f"\nEval set: {result.total} prompts")
    click.echo(
        f"Trigger accuracy: {result.passed}/{result.trigger_scored} "
        f"({result.pass_rate * 100:.0f}%)  "
        f"— does the description fire on the right questions?"
    )
    coverage_scored = (
        result.trigger_questions - len(result.coverage_ambiguous) - len(result.coverage_errors)
    )
    click.echo(
        f"Body coverage:    {result.coverage_passed}/{coverage_scored} "
        f"({result.coverage_rate * 100:.0f}%)  "
        f"— does SKILL.md actually support what the description promises?"
    )

    if result.misses:
        click.echo(f"\nTrigger misses ({len(result.misses)}):")
        for miss in result.misses:
            click.echo(f"  - {miss.label} {miss.prompt.question}")

    if result.coverage_misses:
        click.echo(f"\nCoverage gaps ({len(result.coverage_misses)}):")
        for gap in result.coverage_misses:
            tail = f" — {gap.reason}" if gap.reason else ""
            click.echo(f"  - {gap.prompt.question}{tail}")

    if result.coverage_ambiguous:
        click.echo(
            f"\n[WARN] Coverage grader returned unparseable output on "
            f"{len(result.coverage_ambiguous)} prompt(s) — excluded from "
            f"the body-coverage score. Try a more capable model:"
        )
        for amb in result.coverage_ambiguous:
            tail = f" — {amb.reason}" if amb.reason else ""
            click.echo(f"  - {amb.prompt.question}{tail}")

    if result.trigger_errors or result.coverage_errors:
        click.echo(
            f"\n[WARN] {len(result.trigger_errors)} trigger and "
            f"{len(result.coverage_errors)} coverage grader call(s) "
            f"failed and are excluded from the scores above:"
        )
        for err in result.trigger_errors:
            click.echo(f"  - trigger:  {err.prompt.question} — {err.reason}")
        for err in result.coverage_errors:
            click.echo(f"  - coverage: {err.prompt.question} — {err.reason}")

    if (
        not result.misses
        and not result.coverage_misses
        and not result.coverage_ambiguous
        and not result.trigger_errors
        and not result.coverage_errors
    ):
        click.echo("\nAll prompts graded correctly with full body support.")

    if save_flag and eval_set is None:
        path = save_eval_set(kb_dir, name, result.prompts)
        click.echo(f"\nEval set persisted to {path}")


# ---------------------------------------------------------------------------
# `openkb deck ...` — deck factory (v0.2)
# ---------------------------------------------------------------------------


@cli.group()
def deck():
    """Generate a polished single-file HTML slide deck from the wiki."""


@deck.command("new")
@click.argument("name")
@click.argument("intent")
@click.option(
    "-y",
    "--yes",
    "yes_flag",
    is_flag=True,
    default=False,
    help="Overwrite existing output/decks/<name>/ without prompting.",
)
@click.option(
    "--critique",
    "critique_flag",
    is_flag=True,
    default=False,
    help="Opt-in second-pass review via a critic agent (slower, higher quality).",
)
@click.option(
    "--skill",
    "skill_name",
    metavar="SKILL_NAME",
    default=None,
    # NOTE: 'openkb-deck-neon' below must stay in sync with
    # DEFAULT_DECK_SKILL in openkb/deck/creator.py.
    help=(
        "Which deck skill to use. Defaults to 'openkb-deck-neon' "
        "(the built-in). Pass e.g. 'deck-guizang-editorial' to route to "
        "a third-party skill installed under ~/.openkb/skills/."
    ),
)
@click.pass_context
def deck_new(ctx, name, intent, yes_flag, critique_flag, skill_name):
    """Generate a new HTML deck from this KB's wiki.

    NAME is a kebab-case slug used for the output directory.
    INTENT is a natural-language description of what the deck is about.

    Example:

      openkb deck new transformers-pitch "Explain attention to engineers"
      openkb deck new transformers-pitch "Explain attention to engineers" --critique
      openkb deck new transformers-pitch "..." --skill deck-guizang-editorial
    """
    kb_dir = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if kb_dir is None:
        click.echo("No knowledge base found. Run `openkb init` first.", err=True)
        ctx.exit(1)

    # Reuse the shared safety gates: name validation + wiki content check.
    # Matches chat's `/deck new` so users see the same errors in both UIs.
    err = _preflight_skill_new(kb_dir, name)
    if err:
        # _preflight_skill_new returns messages like "Skill name must not be empty."
        # and "Wiki at ... is empty — add documents with `openkb add` first."
        err = err.replace("Skill name", "Deck name")
        # Only append the kebab-case hint when the failure is actually about
        # the slug, not the wiki-content gate.
        if "kebab" not in err.lower() and "Wiki" not in err and "wiki" not in err:
            err = err + " Use a kebab-case slug like 'my-deck'."
        click.echo(f"[ERROR] {err}", err=True)
        ctx.exit(1)

    # Verify LLM key + load config BEFORE touching existing output. Any
    # failure here (missing API key, malformed config) must leave the old
    # deck directory intact — we can't replace it if we can't proceed.
    try:
        _setup_llm_key(kb_dir)
    except RuntimeError as exc:
        click.echo(f"[ERROR] {exc}", err=True)
        ctx.exit(1)
    config = resolve_effective_config(kb_dir)[0]
    model = config.get("model", DEFAULT_CONFIG["model"])

    from openkb.application.generators import (
        GenerationOptions,
        generate_artifact,
        preview_generation,
    )
    from openkb.deck.creator import DEFAULT_DECK_SKILL

    try:
        preview = preview_generation(kb_dir, "deck", name, skill_name=skill_name)
    except (RuntimeError, ValueError, OSError) as exc:
        click.echo(f"[ERROR] {exc}", err=True)
        ctx.exit(1)
    target_label = preview.target.relative_to(kb_dir.resolve()).as_posix()
    if preview.target.is_dir():
        target_label += "/"
    if preview.exists and not yes_flag:
        if not sys.stdin.isatty():
            click.echo(
                f"[ERROR] {target_label} exists. Pass -y to overwrite in non-interactive contexts.",
                err=True,
            )
            ctx.exit(1)
        if not click.confirm(f"{target_label} already exists. Overwrite?", default=False):
            click.echo("Aborted.")
            ctx.exit(1)
    skill_label = skill_name if skill_name else f"{DEFAULT_DECK_SKILL} (default)"
    click.echo(f"Generating deck '{name}' via skill {skill_label}...")
    gen = asyncio.run(
        generate_artifact(
            kb_dir,
            GenerationOptions(
                "deck",
                name,
                intent,
                overwrite="archive",
                version=preview.version,
                critique=critique_flag,
                skill_name=skill_name,
            ),
            model=model,
        )
    )
    if gen.status != "completed":
        click.echo(f"[ERROR] {gen.message}", err=True)
        ctx.exit(1)

    # Surface validation result.
    if gen.validation:
        for w in gen.validation.warnings:
            click.echo(f"[WARN] {w}", err=True)
        for e in gen.validation.errors:
            click.echo(f"[ERROR] {e}", err=True)
        if gen.validation.errors:
            click.echo(
                f"Deck written to {gen.artifact_path or gen.output_dir / 'index.html'} but failed validation. "
                f"Inspect and re-run.",
                err=True,
            )
            ctx.exit(1)

    click.echo(f"Deck written to {gen.artifact_path or gen.output_dir / 'index.html'}")


def _save_deck_iteration(kb_dir: Path, deck_name: str) -> Path | None:
    """Copy ``<kb>/output/decks/<name>/`` to the next iteration slot.

    Mirrors ``openkb.skill.workspace.save_iteration`` but uses
    ``deck_workspace_dir`` so deck rollback history stays separate from
    skill history. Returns the saved iteration path, or ``None`` if there's
    no current deck to save.
    """
    import re
    from openkb.deck import deck_dir as _deck_dir, deck_workspace_dir as _deck_workspace_dir

    src = _deck_dir(kb_dir, deck_name)
    if not src.is_dir():
        return None

    ws = _deck_workspace_dir(kb_dir, deck_name)
    ws.mkdir(parents=True, exist_ok=True)

    iter_re = re.compile(r"^iteration-(\d+)$")
    existing_ns: list[int] = []
    for child in ws.iterdir():
        if child.is_dir():
            m = iter_re.match(child.name)
            if m:
                existing_ns.append(int(m.group(1)))
    next_n = (max(existing_ns) if existing_ns else 0) + 1

    dest = ws / f"iteration-{next_n}"
    shutil.copytree(src, dest)
    return dest


# ---------------------------------------------------------------------------
# REST API helpers (structured init/list/status/lint + add wrapper)
#
# These return plain dicts (or AddFileResult) so openkb.api can serialize them
# directly as JSON. They deliberately reuse the locked CLI code paths so the
# API and CLI never diverge in behavior.
# ---------------------------------------------------------------------------


from openkb.api_lint import fix_summary

_fix_summary = fix_summary


async def run_lint_report(
    kb_dir: Path, *, fix: bool = False, echo: bool = False, bundle=None
) -> dict:
    """Compatibility export for callers of the former CLI business helper."""
    from openkb.api_lint import run_lint_report as report

    return await report(
        kb_dir,
        fix=fix,
        echo=echo,
        bundle=bundle,
        prepare_model=(lambda: _setup_llm_key(kb_dir)) if bundle is None else None,
    )
