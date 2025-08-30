# tests/test_gen_snapshot_and_format.py

import sys
import textwrap
import json
import logging
from pathlib import Path
import pytest

from pytead.cli.service_cli import collect_and_emit_tests
from pytead.gen_tests import _get_param_info, _resolve_callable_with_submodules
# Assume mock_project fixture is defined in conftest.py or similar
# For clarity, here is a possible implementation of mock_project
from pytest import fixture

@fixture
def mock_project(tmp_path: Path) -> Path:
    """Crée une structure de projet de test."""
    project_src = tmp_path / "my_project"
    project_src.mkdir()
    (project_src / "__init__.py").touch()
    
    code_content = textwrap.dedent("""
        class MyObject:
            pass
        class MyClass:
            def simple_method(self, obj: 'MyObject') -> str:
                return 'ok'
        def simple_function(val: int) -> int:
            return val + 1
    """)
    (project_src / "code.py").write_text(code_content, encoding='utf-8')

    utils_dir = project_src / "utils"
    utils_dir.mkdir()
    (utils_dir / "__init__.py").touch()

    calculator_code = textwrap.dedent("""
        from helpers import Vector
        class GameObject:
            def __init__(self, name: str, position: 'Vector'):
                self.name = name
                self.position = position
        def add_objects(obj1: GameObject, obj2: GameObject) -> GameObject:
            return obj1
    """)
    (utils_dir / "calculator.py").write_text(calculator_code, encoding='utf-8')

    orphan_lib = tmp_path / "orphan_lib"
    orphan_lib.mkdir()
    
    helpers_code = textwrap.dedent("""
        class Vector:
            def __init__(self, x: int, y: int):
                self.x = x
                self.y = y
    """)
    (orphan_lib / "helpers.py").write_text(helpers_code, encoding='utf-8')

    return tmp_path

# --- Tests ---

def test_introspection_on_simple_function(mock_project: Path):
    """
    TEST 1 (simple) : Vérifie que l'introspection fonctionne sur une fonction simple.
    """
    original_sys_path = sys.path[:]
    sys.path.insert(0, str(mock_project))
    sys.path.insert(0, str(mock_project / "orphan_lib"))
    try:
        param_types, imports_needed = _get_param_info("my_project.code.simple_function", is_method=False)
        
        # ASSERTIONS
        # Le code doit identifier les types, même natifs.
        assert param_types == {'val': int}
        # Mais les types natifs ne nécessitent pas d'importation.
        assert not imports_needed
    finally:
        sys.path[:] = original_sys_path
        # Nettoyage pour éviter la pollution des tests
        for mod in ["my_project.code", "my_project"]:
            if mod in sys.modules:
                del sys.modules[mod]

def test_introspection_on_method(mock_project: Path):
    """
    TEST 2 (ciblé) : Vérifie que l'introspection gère une méthode,
    ignore 'self', et trouve les types simples et imbriqués.
    """
    original_sys_path = sys.path[:]
    sys.path.insert(0, str(mock_project))
    sys.path.insert(0, str(mock_project / "orphan_lib"))
    try:
        func_fqn = "my_project.code.MyClass.simple_method"
        param_types, imports_needed = _get_param_info(func_fqn, is_method=True)
    
        # ASSERTIONS
        assert "obj" in param_types
        MyObject_type = param_types["obj"]
        assert MyObject_type.__name__ == "MyObject"
        assert MyObject_type.__module__ == "my_project.code"
        assert imports_needed == {"my_project.code": {"MyObject"}}
    finally:
        sys.path[:] = original_sys_path
        # Nettoyage pour éviter la pollution des tests
        for mod in ["my_project.code", "my_project"]:
            if mod in sys.modules:
                del sys.modules[mod]

def test__get_param_info_method_smoke(mock_project: Path):
    original_sys_path = sys.path[:]
    sys.path.insert(0, str(mock_project))
    sys.path.insert(0, str(mock_project / "orphan_lib"))
    try:
        from pytead.gen_tests import _get_param_info
        t, imp = _get_param_info("my_project.code.MyClass.simple_method")
        assert "obj" in t and getattr(t["obj"], "__name__", "") == "MyObject"
    finally:
        sys.path[:] = original_sys_path
        for mod in ["my_project.code", "my_project"]:
            if mod in sys.modules:
                del sys.modules[mod]

def test__resolve_callable_with_submodules_smoke(tmp_path: Path, caplog):
    # Arrange
    pkg = tmp_path / "my_project"
    pkg.mkdir(); (pkg / "__init__.py").touch()
    (pkg / "code.py").write_text(
        "class MyObject:\n    pass\n"
        "class MyClass:\n"
        "    def simple_method(self, obj: 'MyObject') -> str:\n"
        "        return 'ok'\n",
        encoding="utf-8"
    )
    original_sys_path = sys.path[:]
    sys.path.insert(0, str(tmp_path))
    caplog.set_level(logging.DEBUG, logger="pytead.gen")
    try:
        from pytead.gen_tests import _resolve_callable_with_submodules
        fn, mod, owner = _resolve_callable_with_submodules("my_project.code.MyClass.simple_method")
        assert fn.__name__ == "simple_method"
        assert mod.__name__ == "my_project.code"
        assert owner.__name__ == "MyClass"
    finally:
        sys.path[:] = original_sys_path
        for mod in ["my_project.code", "my_project"]:
            if mod in sys.modules:
                del sys.modules[mod]

def test__get_param_info_method_intermediate(tmp_path: Path, caplog):
    pkg = tmp_path / "my_project"
    pkg.mkdir(); (pkg / "__init__.py").touch()
    (pkg / "code.py").write_text(
        "class MyObject:\n    pass\n"
        "class MyClass:\n"
        "    def simple_method(self, obj: MyObject) -> str:\n"
        "        return 'ok'\n",
        encoding="utf-8"
    )
    original_sys_path = sys.path[:]
    sys.path.insert(0, str(tmp_path))
    caplog.set_level(logging.DEBUG, logger="pytead.gen")
    try:
        from pytead.gen_tests import _get_param_info
        t, imp = _get_param_info("my_project.code.MyClass.simple_method")
        assert "obj" in t, f"param_types keys: {list(t.keys())}"
    finally:
        sys.path[:] = original_sys_path
        for mod in ["my_project.code", "my_project"]:
            if mod in sys.modules:
                del sys.modules[mod]

def test_snapshot_generation_with_complex_imports_and_formatting(tmp_path: Path):
    """
    Test d'intégration complet : valide la génération pour une fonction avec
    des dépendances complexes et des types imbriqués.
    """
    # ARRANGE
    project_src = tmp_path / "my_project"
    project_src.mkdir()
    (project_src / "__init__.py").touch()
    utils_dir = project_src / "utils"
    utils_dir.mkdir()
    (utils_dir / "__init__.py").touch()
    orphan_lib = tmp_path / "orphan_lib"
    orphan_lib.mkdir()

    calculator_code = textwrap.dedent("""
        from helpers import Vector
        class GameObject:
            def __init__(self, name: str, position: 'Vector'):
                self.name = name
                self.position = position
        def add_objects(obj1: GameObject, obj2: GameObject) -> GameObject:
            return obj1
    """)
    (utils_dir / "calculator.py").write_text(calculator_code, encoding='utf-8')

    helpers_code = textwrap.dedent("""
        class Vector:
            def __init__(self, x: int, y: int):
                self.x = x
                self.y = y
    """)
    (orphan_lib / "helpers.py").write_text(helpers_code, encoding='utf-8')

    storage_dir = tmp_path / "call_logs"
    storage_dir.mkdir()
    output_dir = tmp_path / "generated_tests"
    output_dir.mkdir()

    trace_content = {
        "func": "my_project.utils.calculator.add_objects",
        "args_graph": [{'name': 'player', 'position': {'x': 10, 'y': 20}}, {'name': 'mob', 'position': {'x': 5, 'y': 5}}],
        "kwargs_graph": {}, "result_graph": {'name': 'player', 'position': {'x': 10, 'y': 20}}
    }
    trace_file = storage_dir / "my_project_utils_calculator_add_objects__fake.gjson"
    trace_file.write_text(json.dumps(trace_content))

    # ACT
    import_roots = [str(project_src.parent), str(orphan_lib)]
    original_sys_path = sys.path[:]
    sys.path.insert(0, str(tmp_path))
    sys.path.insert(0, str(orphan_lib))
    
    try:
        result = collect_and_emit_tests(
            storage_dir=storage_dir, formats=["graph-json"],
            output_dir=output_dir,
            import_roots=import_roots, logger=None
        )
    finally:
        sys.path[:] = original_sys_path
        # Nettoyage très large pour ce test complexe
        mods_to_clean = [
            "my_project.utils.calculator", 
            "my_project.utils", 
            "my_project", 
            "helpers"
        ]
        for mod in mods_to_clean:
            if mod in sys.modules:
                del sys.modules[mod]

    # ASSERT
    assert result is not None and result.files_written == 1
    generated_file = output_dir / "test_my_project_utils_calculator_add_objects_snapshots.py"
    assert generated_file.exists()
    content = generated_file.read_text()
    assert "from my_project.utils.calculator import GameObject" in content
    assert "from helpers import Vector" in content
