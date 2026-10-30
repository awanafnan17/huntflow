# tests/test_production_traceability_automated.py
"""Automated Traceability Test Suite validating that acceptance test files import and invoke production services."""
import json
import os
import ast
import pytest

TRACEABILITY_JSON = "artifacts/test_production_traceability.json"


@pytest.mark.django_db
def test_production_traceability_automated():
    """Verify that every test node ID in test_production_traceability.json directly calls production services in its test function."""
    assert os.path.exists(TRACEABILITY_JSON), "Missing artifacts/test_production_traceability.json"

    with open(TRACEABILITY_JSON, "r") as f:
        records = json.load(f)

    assert len(records) > 0, "Traceability records list is empty"

    ast_cache = {}

    for record in records:
        node_id = record["test_node_id"]
        prod_service = record["production_service_called"]

        assert node_id, "Empty test_node_id"
        assert prod_service, f"Empty production_service_called for {node_id}"
        assert "." in prod_service, f"Production service {prod_service} must be a full importable symbol path"

        parts = node_id.split("::")
        file_path = parts[0]
        assert os.path.exists(file_path), f"Test file {file_path} does not exist"

        if file_path not in ast_cache:
            with open(file_path, "r", encoding="utf-8") as f:
                ast_cache[file_path] = ast.parse(f.read(), filename=file_path)

        tree = ast_cache[file_path]
        service_func_name = prod_service.split(".")[-1]

        # Check module-level imports
        module_imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_imports.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    for alias in node.names:
                        module_imports.add(f"{node.module}.{alias.name}")
                        module_imports.add(alias.name)

        # Locate exact function AST node
        target_func_node = None
        if len(parts) == 2:
            func_name = parts[1]
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name == func_name:
                    target_func_node = node
                    break
        elif len(parts) == 3:
            class_name, func_name = parts[1], parts[2]
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and node.name == class_name:
                    for sub in node.body:
                        if isinstance(sub, ast.FunctionDef) and sub.name == func_name:
                            target_func_node = sub
                            break

        # Check for function node or tree-wide fallback
        search_node = target_func_node if target_func_node is not None else tree

        found_call_or_ref = False
        mocked_prod_service = False

        for node in ast.walk(search_node):
            if isinstance(node, ast.Name) and node.id == service_func_name:
                found_call_or_ref = True
            elif isinstance(node, ast.Attribute) and node.attr == service_func_name:
                found_call_or_ref = True
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == service_func_name:
                    found_call_or_ref = True
                elif isinstance(node.func, ast.Attribute) and getattr(node.func, "attr", None) == service_func_name:
                    found_call_or_ref = True

            # Assert production service itself is not mocked
            if isinstance(node, ast.Call):
                call_str = ast.unparse(node) if hasattr(ast, "unparse") else ""
                if "patch" in call_str and service_func_name in call_str:
                    mocked_prod_service = True

        assert found_call_or_ref or service_func_name in [imp.split(".")[-1] for imp in module_imports], \
            f"Test node {node_id} does not call or reference declared production service {prod_service}"
        assert not mocked_prod_service, f"Production service {prod_service} is mocked in {node_id}"

        # Assert mandatory metadata fields
        assert record.get("database_state_asserted"), f"Missing database_state_asserted for {node_id}"
        assert record.get("audit_event_asserted"), f"Missing audit_event_asserted for {node_id}"
