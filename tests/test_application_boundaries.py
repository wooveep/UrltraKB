"""Keep shared use cases independent of CLI, HTTP and desktop entrypoints."""

import ast
from pathlib import Path

import openkb


def test_application_does_not_import_entrypoint_adapters():
    package = Path(openkb.__file__).parent / "application"
    violations = []
    for path in package.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
                if node.module == "openkb":
                    modules.extend(f"openkb.{alias.name}" for alias in node.names)
            for module in modules:
                leaf = module.removeprefix("openkb.").split(".")[0]
                if module.startswith("openkb.") and (
                    leaf in {"cli", "api", "desktop", "terminal_answers", "terminal_maintenance"}
                    or leaf.startswith(("cli_", "api_"))
                ):
                    violations.append(f"{path.name}:{node.lineno}: {module}")
    assert not violations, "Move business behavior into application use cases: " + "; ".join(
        violations
    )
