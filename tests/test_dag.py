"""The Airflow DAG must import cleanly and wire its tasks in the right order.

Installing Airflow in CI is heavy, so this test swaps in a tiny fake `airflow` package
whose @dag/@task decorators just record what the DAG file does. It catches the bugs
that matter most: import errors, renamed functions and broken task wiring.
"""
import importlib.util
import sys
import types

from src import config


def test_dag_wiring(monkeypatch):
    calls: list[tuple[str, tuple]] = []
    dag_kwargs: dict = {}

    def task(fn):
        def wrapper(*args):
            calls.append((fn.__name__, args))
            return f"<{fn.__name__} output>"
        return wrapper

    def dag(**kwargs):
        dag_kwargs.update(kwargs)
        return lambda fn: fn

    airflow = types.ModuleType("airflow")
    decorators = types.ModuleType("airflow.decorators")
    decorators.dag, decorators.task = dag, task
    operators = types.ModuleType("airflow.operators")
    python = types.ModuleType("airflow.operators.python")
    python.get_current_context = lambda: {}
    for name, mod in {"airflow": airflow, "airflow.decorators": decorators,
                      "airflow.operators": operators, "airflow.operators.python": python}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setitem(sys.modules, "airflow.sdk", None)  # force the Airflow 2.x import path

    spec = importlib.util.spec_from_file_location("grid_pulse_dag", config.ROOT / "dags" / "grid_pulse_dag.py")
    spec.loader.exec_module(importlib.util.module_from_spec(spec))

    order = [name for name, _ in calls]
    assert order[:4] == ["extract", "transform", "quality", "load"]
    assert set(order[4:]) == {"enrich_weather", "detect_anomalies"}
    # Each stage receives the previous stage's output (file paths travel through XCom).
    assert dict(calls)["transform"] == ("<extract output>",)
    assert dict(calls)["load"] == ("<quality output>",)
    assert dag_kwargs["dag_id"] == "grid_pulse" and dag_kwargs["max_active_runs"] == 1
