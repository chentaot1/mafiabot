import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "bot.py"


EXPECTED_CALL_ORDER = ["run_night_pipeline"]


EXPECTED_WRAPPERS = {
    "_resolve_transports": ("await", "night_engine.resolve_transports"),
    "_resolve_control": ("await", "night_engine.resolve_control"),
    "_build_visit_log": ("return", "night_engine.build_visit_log"),
    "_resolve_blocking": ("return", "night_engine.resolve_blocking"),
    "_apply_misc_actions": ("return_await", "night_engine.apply_misc_actions"),
    "_resolve_investigative": ("await", "night_engine.resolve_investigative"),
    "_resolve_killing": ("return_await", "night_engine.resolve_killing"),
    "_send_night_feedback": ("await", "night_engine.send_night_feedback"),
}


def dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        if base is None:
            return None
        return f"{base}.{node.attr}"
    return None


def find_func(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise SystemExit(f"Could not find function {name!r} at module level.")


def find_method_in_class(tree: ast.Module, class_name: str, method: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for n in tree.body:
        if isinstance(n, ast.ClassDef) and n.name == class_name:
            for b in n.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)) and b.name == method:
                    return b
    raise SystemExit(f"Could not find {class_name}.{method}.")


def extract_ordered_pipeline_calls(resolve_fn: ast.AST) -> list[str]:
    calls: list[str] = []
    for n in ast.walk(resolve_fn):
        if isinstance(n, ast.Call):
            dn = dotted_name(n.func)
            if dn in EXPECTED_CALL_ORDER:
                calls.append(dn)
    # Preserve appearance order by source position
    calls = sorted(
        calls,
        key=lambda name: next(
            (
                c.lineno
                for c in ast.walk(resolve_fn)
                if isinstance(c, ast.Call) and dotted_name(c.func) == name
            ),
            10**9,
        ),
    )
    # The above sorting by first lineno per call name doesn't handle duplicates,
    # but these calls should each appear exactly once.
    return calls


def check_resolve_call_order(tree: ast.Module) -> None:
    resolve_fn = None
    for n in tree.body:
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "evaluate":
            resolve_fn = n
            break
    if resolve_fn is None:
        raise SystemExit("Could not find async function 'resolve'.")

    # Extract calls in source order using (lineno, col_offset).
    seen: list[tuple[int, int, str]] = []
    for n in ast.walk(resolve_fn):
        if isinstance(n, ast.Call):
            dn = dotted_name(n.func)
            if dn in EXPECTED_CALL_ORDER and hasattr(n, "lineno"):
                seen.append((n.lineno, getattr(n, "col_offset", 0), dn))

    seen.sort()
    ordered: list[str] = [dn for _, __, dn in seen]

    if ordered != EXPECTED_CALL_ORDER:
        raise SystemExit(
            "Resolve pipeline order mismatch.\n"
            f"Expected:\n  {EXPECTED_CALL_ORDER}\n"
            f"Got:\n  {ordered}\n"
        )


def check_wrapper(tree: ast.Module, method: str, expectation: tuple[str, str]) -> None:
    mode, target = expectation
    fn = find_method_in_class(tree, "Game", method)
    body = fn.body

    # Allow optional docstring-only first statement (not expected, but tolerated).
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]

    if mode == "await":
        if len(body) != 1 or not isinstance(body[0], ast.Expr) or not isinstance(body[0].value, ast.Await):
            raise SystemExit(f"{method} is not a single await delegate.")
        call = body[0].value.value
        if not isinstance(call, ast.Call) or dotted_name(call.func) != target:
            raise SystemExit(f"{method} does not await {target}.")
    elif mode == "return":
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            raise SystemExit(f"{method} is not a single return delegate.")
        if not isinstance(body[0].value, ast.Call) or dotted_name(body[0].value.func) != target:
            raise SystemExit(f"{method} does not return {target}(..).")
    elif mode == "return_await":
        if len(body) != 1 or not isinstance(body[0], ast.Return) or not isinstance(body[0].value, ast.Await):
            raise SystemExit(f"{method} is not a single 'return await' delegate.")
        call = body[0].value.value
        if not isinstance(call, ast.Call) or dotted_name(call.func) != target:
            raise SystemExit(f"{method} does not return await {target}(..).")
    else:
        raise SystemExit(f"Unknown wrapper mode: {mode}")


def check_wrappers(tree: ast.Module) -> None:
    for method, expectation in EXPECTED_WRAPPERS.items():
        check_wrapper(tree, method, expectation)


def main() -> None:
    src = (ROOT / "gameplay/resolution.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(BOT))

    check_resolve_call_order(tree)
    check_wrappers(ast.parse((ROOT / "game.py").read_text(encoding="utf-8")))
    print("OK: resolve() call order and Game wrappers match expectations.")


if __name__ == "__main__":
    main()
