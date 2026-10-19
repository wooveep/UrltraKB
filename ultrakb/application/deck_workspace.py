"""Private Deck tool outputs reconstructed from retained SDK responses."""

from pathlib import PurePosixPath
from threading import RLock

from agents import function_tool

from ultrakb.locks import atomic_write_bytes
from ultrakb.storage.artifact_store import ArtifactStore, artifact_path
from ultrakb.storage.wiki_paths import wiki_path


class DeckWorkspace:
    def __init__(self, scope, directory, target, epoch, context):
        self.scope, self.directory, self.target = scope, directory, target
        self.epoch, self.context = epoch, context
        self.artifacts = ArtifactStore(scope.kb_dir, scope.knowledge.identity)
        self.files, self.quality, self.guard = {}, [], RLock()
        self.active = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        with self.guard:
            self.active = False

    def _check_active(self):
        if not self.active:
            raise RuntimeError("The generated artifact writer has finished")
        self.context.check_stop()

    def _existing(self, path):
        try:
            if path.startswith("wiki/"):
                self.scope.knowledge.file_digest(path.removeprefix("wiki/"))
            else:
                self.artifacts.entry(path, epoch=self.epoch)
        except FileNotFoundError:
            return False
        return True

    def _protected(self, path):
        parts = PurePosixPath(path).parts
        if any(
            part.endswith("-workspace")
            and index + 1 < len(parts)
            and parts[index + 1].startswith("iteration-")
            for index, part in enumerate(parts)
        ):
            return "history_write_denied"
        if path != self.target and not path.startswith(self.target + "/") and self._existing(path):
            return "unconfirmed_output_write_denied"
        return None

    def write(self, path, content):
        with self.guard:
            self._check_active()
            try:
                wiki_path(path)
            except ValueError:
                return "Access denied: output requires a logical relative file path."
            if not path.startswith(("output/", "wiki/explorations/")):
                return "Access denied: output requires output/ or wiki/explorations/ files."
            if path.startswith("output/"):
                artifact_path(path)
            issue = self._protected(path)
            if issue:
                if issue not in self.quality:
                    self.quality.append(issue)
                return "Access denied: existing files outside the selected artifact are read-only."
            payload = content.encode("utf-8")
            atomic_write_bytes(self.directory / path, payload)
            self.files[path] = payload
            return f"Written: {path}"

    def read(self, path):
        try:
            wiki_path(path)
        except ValueError:
            return "Access denied: invalid logical file path."
        if not path.startswith(("output/", "skills/")):
            return "Use the permitted wiki readers for knowledge evidence."
        with self.guard:
            self._check_active()
            if path in self.files:
                return self.files[path].decode("utf-8", errors="replace")
            try:
                if path.startswith("output/"):
                    return self.artifacts.read(path, epoch=self.epoch).decode(
                        "utf-8", errors="replace"
                    )
                root = self.scope.kb_dir
                target = (root / path).resolve()
                if not target.is_relative_to(root / "skills"):
                    return "Access denied: skill file escapes its directory."
                return target.read_text("utf-8", errors="replace")
            except FileNotFoundError:
                return f"File not found: {path}"

    def overlay(self):
        from .storage_generation_replacement import generation_paths, read_generation_file

        for path in generation_paths(self.scope, self.target, self.epoch):
            payload = read_generation_file(self.scope, path, self.epoch)
            atomic_write_bytes(self.directory / path, payload)
            self.files[path] = payload

    def tools(self):
        @function_tool(failure_error_function=None)
        def write_file(path: str, content: str) -> str:
            """Write a complete text file under output/ or wiki/explorations/."""
            return self.write(path, content)

        @function_tool(failure_error_function=None)
        def read_output_or_skill_file(path: str) -> str:
            """Read a selected output or local skill file; use wiki tools for knowledge."""
            return self.read(path)

        return write_file, read_output_or_skill_file
