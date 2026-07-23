"""Process introspection tests against our own running process."""
import os

from synapse.re import processes


def test_list_processes_contains_self():
    procs = processes.list_processes()
    assert len(procs) > 5
    pids = [p["pid"] for p in procs]
    assert os.getpid() in pids
    me = next(p for p in procs if p["pid"] == os.getpid())
    assert me["mem_mb"] > 0
    assert "connections" in me


def test_own_process_detail():
    detail = processes.process_detail(os.getpid())
    info = detail["info"]
    assert info["pid"] == os.getpid()
    assert info["num_threads"] >= 1
    assert isinstance(detail["threads"], list) and detail["threads"]
    # our own modules should include the python executable
    names = [m.get("name", "").lower() for m in detail["modules"] if "name" in m]
    assert any("python" in n for n in names)
    # regions summary populated for own process
    assert detail["regions"]["count"] > 0
    assert detail["regions"]["by_type"]


def test_modules_have_base_and_size():
    mods = processes.list_modules(os.getpid())
    assert mods
    first = mods[0]
    assert first["base"] > 0
    assert first["size"] > 0
    assert first["path"]
