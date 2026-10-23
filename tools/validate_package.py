"""Check packaged Python imports and web references without importing application code."""
from __future__ import annotations

import ast
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


class AssetReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "img", "source"} and attrs.get("src"):
            self.references.append(attrs["src"])
        if tag == "link" and attrs.get("href"):
            rel = set(attrs.get("rel", "").split())
            if rel & {"stylesheet", "icon", "modulepreload", "preload"}:
                self.references.append(attrs["href"])


def validate(root: Path, files: set[str]) -> None:
    module_files = {}
    modules = {}
    for name in sorted(files):
        if not name.startswith("app/") or not name.endswith(".py"):
            continue
        module = name[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module.removesuffix(".__init__")
        module_files[module] = name

    def parsed(module):
        if module not in modules:
            name = module_files[module]
            modules[module] = (name, ast.parse(
                (root / name).read_text(encoding="utf-8-sig"), filename=name))
        return modules[module]

    # Follow reviewed server/operator roots; reject orphan source without reading its content.
    pending = {"app", "app.main", "app.retention"} & module_files.keys()
    seen = set()
    while pending:
        module = min(pending)
        pending.remove(module)
        if module in seen:
            continue
        seen.add(module)
        name, tree = parsed(module)
        package = module if name.endswith("/__init__.py") else module.rpartition(".")[0]
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parents = package.split(".")
                    if node.level > len(parents):
                        raise ValueError(f"Import escapes the package: {name}:{node.lineno}")
                    base = ".".join(parents[:len(parents) - node.level + 1])
                    target = ".".join(filter(None, (base, node.module)))
                else:
                    target = node.module or ""
                targets = [target]
                # `from . import billing` loads a child module, not just app/__init__.py.
                if target in module_files and module_files[target].endswith("/__init__.py"):
                    exported = set()
                    for declaration in parsed(target)[1].body:
                        if isinstance(declaration, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            exported.add(declaration.name)
                        elif isinstance(declaration, (ast.Import, ast.ImportFrom)):
                            exported.update(alias.asname or alias.name.split(".")[0]
                                            for alias in declaration.names)
                        elif isinstance(declaration, (ast.Assign, ast.AnnAssign)):
                            assigned = declaration.targets if isinstance(declaration, ast.Assign) else [declaration.target]
                            exported.update(n.id for assignment in assigned
                                            for n in ast.walk(assignment) if isinstance(n, ast.Name))
                    targets += [f"{target}.{alias.name}" for alias in node.names
                                if alias.name != "*" and alias.name not in exported]
            for target in targets:
                if target == "app" or target.startswith("app."):
                    if target not in module_files:
                        raise ValueError(f"Missing Python module: {name}:{node.lineno} -> {target}")
                    pending.add(target)
    if unexpected := module_files.keys() - seen:
        raise ValueError("Unexpected unreferenced Python source: " + ", ".join(sorted(unexpected)))

    for name in sorted(files):
        references = []
        if name.endswith(".html"):
            parser = AssetReferences()
            parser.feed((root / name).read_text(encoding="utf-8"))
            references = parser.references
        elif name.endswith(".css"):
            text = (root / name).read_text(encoding="utf-8")
            # An inline SVG data URI carries its own quoting and its own url(#id) filter
            # references. Both confuse the pattern below -- it stops at the first quote or
            # bracket inside the payload and reports the fragment as a missing file. Drop
            # the data URIs first: they embed their asset rather than referring to one.
            text = re.sub(r"""url\(\s*(?:"data:[^"]*"|'data:[^']*'|data:[^)]*)\s*\)""", "url()", text)
            references = [match[1] for match in re.findall(
                r"""url\(\s*(['"]?)([^)'"]+)\1\s*\)""", text)]
        for reference in references:
            url = urlsplit(reference.strip())
            if url.scheme or url.netloc or not url.path:
                continue
            path = unquote(url.path)
            # url(#gradient) points at an element in the same document, not at a file.
            if path.startswith("#"):
                continue
            base = PurePosixPath("web") if path.startswith("/") else PurePosixPath(name).parent
            parts = list(base.parts)
            for part in PurePosixPath(path.lstrip("/")).parts:
                if part == "..":
                    if len(parts) <= 1:
                        raise ValueError(f"Asset reference escapes web/: {name} -> {reference}")
                    parts.pop()
                elif part != ".":
                    parts.append(part)
            target = "/".join(parts)
            if target not in files:
                raise ValueError(f"Missing web asset: {name} -> {reference}")


if __name__ == "__main__":
    try:
        validate(Path(sys.argv[1]), set(json.load(sys.stdin)))
    except (OSError, SyntaxError, ValueError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from None
