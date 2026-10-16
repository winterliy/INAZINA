"""Layer rules for managers/ (see docs/architecture.md).

A module may import its own layer or any lower one, never a higher one.
Imports inside functions count too: deferring an import hides the dependency
from readers but not from this test.
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAYERS = {
    0: {"_price_math", "price_backend", "stochastic_price_engine", "auto_target_engine",
        "movement_history_manager", "order_validation", "market_session_manager"},
    1: {"currency_manager", "rate_manager", "financial_manager"},
    2: {"bank_manager", "interest_manager", "currency_exchange_manager", "treasury_bond_manager",
        "option_manager", "futures_manager", "els_manager", "fund_manager", "investment_manager",
        "fractional_trading_manager", "foreign_securities_manager", "corporate_actions_manager",
        "order_book_manager", "lottery_manager", "user_manager", "nationality_manager", "market_manager"},
    3: {"admin_manager", "admin_control_manager", "admin_commands_manager", "catalog_manager"},
}
LAYER_OF = {module: layer for layer, modules in LAYERS.items() for module in modules}


def manager_imports(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names = [node.module] if isinstance(node, ast.ImportFrom) and node.module else \
            [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
        for name in names:
            if name.startswith("managers."):
                yield name.split(".")[1]


class ArchitectureTests(unittest.TestCase):
    def test_every_manager_module_has_a_layer(self):
        modules = {path.stem for path in (ROOT / "managers").glob("*.py") if path.stem != "__init__"}
        self.assertEqual(sorted(modules - LAYER_OF.keys()), [], "docs/architecture.md에 계층을 정하고 LAYERS에 추가하세요.")

    def test_lower_layers_never_import_higher_layers(self):
        violations = []
        for path in sorted((ROOT / "managers").glob("*.py")):
            own = LAYER_OF.get(path.stem)
            for target in manager_imports(path):
                if own is not None and LAYER_OF.get(target, -1) > own:
                    violations.append(f"{path.stem} (L{own}) → {target} (L{LAYER_OF[target]})")
        self.assertEqual(violations, [])

    def test_database_package_does_not_depend_on_managers(self):
        offenders = [path.name for path in (ROOT / "database").glob("*.py") if any(manager_imports(path))]
        self.assertEqual(offenders, [])
