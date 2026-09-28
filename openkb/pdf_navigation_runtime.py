"""Run the pinned PageIndex PDF algorithm in a per-import function namespace.

Only model dispatch, bounded content failure handling and logging are adapted.
The dependency's TOC branches, 60% verification threshold, three local correction
rounds, page-start decisions and recursive node splitting execute unchanged.
No installed module or process-global model function is monkeypatched.
"""

import asyncio
import inspect
import logging
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
from importlib import import_module
from types import FunctionType

from openkb.pdf_navigation_requests import PDFContentError

logger = logging.getLogger(__name__)

SOURCE_ARGUMENTS = {
    "toc_detector_single_page": "content",
    "extract_toc_content": "content",
    "toc_index_extractor": "content",
    "add_page_number_to_toc": "part",
    "generate_toc_init": "part",
    "generate_toc_continue": "part",
    "single_toc_item_index_fixer": "content",
}
DERIVED_TASKS = {
    "check_if_toc_extraction_is_complete",
    "check_if_toc_transformation_is_complete",
    "detect_page_index",
    "toc_transformer",
    "generate_doc_description",
}


class LegacyPDF:
    def __init__(self, pages, requests):
        self.pages, self.requests = pages, requests
        self.current = ContextVar("pdf_navigation_task", default=None)
        self.expansion_path = ContextVar("pdf_expansion_path", default=())
        self.issues = []
        core = import_module("pageindex.index.page_index")
        utils = import_module("pageindex.index.utils")
        self.functions = {**vars(utils), **vars(core)}
        for name, value in list(self.functions.items()):
            if isinstance(value, FunctionType) and value.__module__ in {
                core.__name__,
                utils.__name__,
            }:
                clone = FunctionType(
                    value.__code__, self.functions, name, value.__defaults__, value.__closure__
                )
                clone.__kwdefaults__ = value.__kwdefaults__
                self.functions[name] = clone
        for name in (
            SOURCE_ARGUMENTS.keys()
            | DERIVED_TASKS
            | {"check_title_appearance", "check_title_appearance_in_start", "generate_node_summary"}
        ):
            self.functions[name] = self._task(name, self.functions[name])
        self.functions["llm_completion"] = self._completion
        self.functions["llm_acompletion"] = self._acompletion
        self.functions["print"] = lambda *args, **kwargs: logger.debug(
            "%s", " ".join(map(str, args))
        )
        self.functions["meta_processor"] = self._meta(self.functions["meta_processor"])
        add_offset = self.functions["add_page_offset_to_toc_json"]

        def offset_or_fallback(data, offset):
            if offset is None:
                raise PDFContentError("pdf_toc_offset_unavailable")
            return add_offset(data, offset)

        self.functions["add_page_offset_to_toc_json"] = offset_or_fallback
        repair = self.functions["fix_incorrect_toc_with_retries"]

        async def repair_with_diagnostics(*args, **kwargs):
            result, remaining = await repair(*args, **kwargs)
            if remaining:
                self.issues.append(
                    {
                        "task": "title_repair",
                        "reason": "unconfirmed_titles",
                        "count": len(remaining),
                    }
                )
            return result, remaining

        self.functions["fix_incorrect_toc_with_retries"] = repair_with_diagnostics
        self.functions["process_large_node_recursively"] = self._expand(
            self.functions["process_large_node_recursively"]
        )

    def _scope(self, name, arguments):
        original, numbers = "", []
        if name in SOURCE_ARGUMENTS:
            original = arguments[SOURCE_ARGUMENTS[name]]
            numbers = self.pages.numbers(original)
        elif name == "check_title_appearance":
            number = arguments["item"].get("physical_index")
            if type(number) is int and 1 <= number <= len(self.pages.values):
                numbers = [number]
                original = self.pages.values[number - 1][0]
        elif name == "check_title_appearance_in_start":
            original = arguments["page_text"]
            numbers = self.pages.numbers(original)
        elif name == "generate_node_summary":
            node = arguments["node"]
            original = node["text"]
            numbers = list(range(node["start_index"], node["end_index"] + 1))
        # Transformed TOCs and prior trees stay in the suffix as derived data.
        return name, original if numbers else "", numbers

    def _task(self, name, function):
        signature = inspect.signature(function)

        def scope(args, kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            return self._scope(name, bound.arguments)

        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def asynchronous(*args, **kwargs):
                selected = scope(args, kwargs)
                token = self.current.set(selected)
                try:
                    return await function(*args, **kwargs)
                except PDFContentError as exc:
                    self.issues.append({"task": name, "pages": selected[2], "reason": str(exc)})
                    if name == "generate_node_summary":
                        return ""
                    if name == "check_title_appearance_in_start":
                        return "no"
                    if name == "check_title_appearance":
                        bound = signature.bind(*args, **kwargs)
                        item = bound.arguments["item"]
                        return {
                            "list_index": item.get("list_index"),
                            "answer": "no",
                            "title": item["title"],
                            "page_number": item.get("physical_index"),
                        }
                    if name == "single_toc_item_index_fixer":
                        return None
                    raise
                finally:
                    self.current.reset(token)

            return asynchronous

        @wraps(function)
        def synchronous(*args, **kwargs):
            selected = scope(args, kwargs)
            token = self.current.set(selected)
            try:
                return function(*args, **kwargs)
            except PDFContentError as exc:
                self.issues.append({"task": name, "pages": selected[2], "reason": str(exc)})
                if name in {"generate_toc_init", "generate_toc_continue"}:
                    return []
                if name == "toc_detector_single_page":
                    return "no"
                raise
            except Exception as exc:
                if type(exc) is Exception and str(exc) in {
                    "Failed to complete table of contents extraction after maximum retries",
                    "Failed to complete TOC transformation after maximum retries",
                }:
                    raise PDFContentError("pdf_toc_continuation_exhausted") from exc
                raise
            finally:
                self.current.reset(token)

        return synchronous

    def _completion(self, model, prompt, chat_history=None, return_finish_reason=False):
        scope = self.current.get()
        if scope is None:
            raise RuntimeError("Legacy PDF model call has no task binding")
        result = self.requests.call(scope, prompt, chat_history)
        return (result, "finished") if return_finish_reason else result

    async def _acompletion(self, model, prompt):
        return await asyncio.to_thread(self._completion, model, prompt)

    def _meta(self, function):
        signature = inspect.signature(function)

        @wraps(function)
        async def wrapped(*args, **kwargs):
            try:
                return await function(*args, **kwargs)
            except PDFContentError as exc:
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                mode = bound.arguments["mode"]
                next_mode = {
                    "process_toc_with_page_numbers": "process_toc_no_page_numbers",
                    "process_toc_no_page_numbers": "process_no_toc",
                }.get(mode)
                if next_mode is None:
                    raise
                self.issues.append({"task": mode, "reason": str(exc)})
                bound.arguments["mode"] = next_mode
                return await wrapped(*bound.args, **bound.kwargs)
            except Exception as exc:
                # The pinned legacy algorithm uses this exact error for failed
                # content verification. Infrastructure/program errors propagate.
                if type(exc) is Exception and str(exc) == "Processing failed":
                    raise PDFContentError("pdf_structure_verification_failed") from exc
                raise

        return wrapped

    def _expand(self, function):
        signature = inspect.signature(function)

        @wraps(function)
        async def wrapped(node, *args, **kwargs):
            original = deepcopy(node)
            bounds = (node["start_index"], node["end_index"])
            path = self.expansion_path.get()
            bound = signature.bind(node, *args, **kwargs)
            bound.apply_defaults()
            options, pages = bound.arguments["opt"], bound.arguments["page_list"]
            expanding = (
                bounds[1] - bounds[0] > options.max_page_num_each_node
                and sum(page[1] for page in pages[bounds[0] - 1 : bounds[1]])
                >= options.max_token_num_each_node
            )
            if expanding and bounds in path:
                self.issues.append({"task": "recursive_expansion", "reason": "no_page_progress"})
                return node
            token = self.expansion_path.set((*path, bounds) if expanding else path)
            try:
                return await function(node, *args, **kwargs)
            except PDFContentError as exc:
                node.clear()
                node.update(original)
                self.issues.append(
                    {
                        "task": "recursive_expansion",
                        "title": node["title"],
                        "pages": [node["start_index"], node["end_index"]],
                        "reason": str(exc),
                    }
                )
                return node
            finally:
                self.expansion_path.reset(token)

        return wrapped

    async def run(self, model, summaries):
        from pageindex import IndexConfig

        options = IndexConfig(model=model)
        tree = await self.functions["tree_parser"](self.pages.values, options, logger=logger)
        for node in self.functions["structure_to_list"](tree):
            if node["end_index"] == node["start_index"] - 1:
                # Two titles can start on the same page. Preserve a readable
                # coarse page instead of publishing the legacy empty range.
                node["end_index"] = node["start_index"]
                self.issues.append({"task": "page_boundary", "reason": "shared_title_page"})
        if summaries:
            self.functions["add_node_text"](tree, self.pages.values)
            await self.functions["generate_summaries_for_structure"](tree, model=model)
        description = ""
        if summaries:
            clean = self.functions["create_clean_structure_for_description"](tree)
            try:
                description = await asyncio.to_thread(
                    self.functions["generate_doc_description"], clean, model=model
                )
            except PDFContentError as exc:
                self.issues.append({"task": "document_description", "reason": str(exc)})
        return tree, description

    def reading_groups(self):
        parts = [
            f"<physical_index_{i}>\n{text}\n<physical_index_{i}>\n\n"
            for i, (text, _) in enumerate(self.pages.values, 1)
        ]
        sizes = [
            self.functions["count_tokens"](text, self.requests.settings["model"]) for text in parts
        ]
        return [
            self.pages.numbers(text)
            for text in self.functions["page_list_to_group_text"](parts, sizes)
        ]
