"""Index public Python SDK declarations from the source revision pinned by this project."""

import argparse
import ast
import json
from pathlib import Path
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def declarations(nodes, prefix, path):
    for node in nodes:
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) or node.name.startswith("_"):
            continue
        name = f"{prefix}.{node.name}"
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(base) for base in node.bases)
            signature = f"class {node.name}" + (f"({bases})" if bases else "")
            kind = "class"
        else:
            arguments = ast.unparse(node.args)
            returns = " -> " + ast.unparse(node.returns) if node.returns else ""
            signature = f"{node.name}({arguments}){returns}"
            kind = "async function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Name) and decorator.id == "property":
                    kind = "property"
                if isinstance(decorator, ast.Attribute) and decorator.attr == "setter":
                    name += ".fset"
                    kind = "property setter"
        yield {"name": name, "kind": kind, "signature": signature, "path": path, "line": node.lineno,
               "doc": ast.get_docstring(node) or ""}
        if isinstance(node, ast.ClassDef):
            yield from declarations(node.body, name, path)


def build(checkout):
    dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    version = next(d.removeprefix("vastai==") for d in dependencies if d.startswith("vastai=="))

    def git(*args):
        return subprocess.check_output(["git", "-C", str(checkout), *args])

    revision = git("rev-parse", f"v{version}^{{commit}}").decode().strip()
    paths = git("ls-tree", "-r", "--name-only", revision, "vastai").decode().splitlines()
    symbols = []
    for path in paths:
        if not path.endswith(".py") or path.startswith(("vastai/cli/", "vastai/pdf/")):
            continue
        module = path[:-3].replace("/", ".").removesuffix(".__init__")
        if any(part.startswith("_") for part in module.split(".")):
            continue
        source = git("show", f"{revision}:{path}")
        symbols.extend(declarations(ast.parse(source).body, module, path))
    return {"version": version, "revision": revision, "symbols": symbols}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    args = parser.parse_args()
    index = build(args.checkout)
    target = ROOT / "src/vastai_mcp/sdk-index.json"
    target.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(index['symbols'])} public declarations from {index['revision']} -> {target}")
