import sqlite3

import pytest

from firm_runtime.store import Store


def value(db):
    return db.execute("SELECT next_number FROM employee_sequence").fetchone()[0]


def test_nested_reads_share_snapshot_while_independent_writer_commits(tmp_path):
    reader = Store.scratch(tmp_path / "firm.sqlite3")
    writer = Store.scratch(reader.path)
    with reader.read() as outer:
        assert value(outer) == 1
        with writer.transaction() as db:
            db.execute("UPDATE employee_sequence SET next_number=2")
        with reader.read() as nested:
            assert value(nested) == 1
            assert nested is outer
        assert value(outer) == 1
    with reader.read() as db:
        assert value(db) == 2


def test_read_scope_rejects_writes_and_transaction_upgrade(tmp_path):
    store = Store.scratch(tmp_path / "firm.sqlite3")
    with store.read() as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("UPDATE employee_sequence SET next_number=2")
        with pytest.raises(RuntimeError, match="read.*transaction"):
            with store.transaction():
                pytest.fail("read scope must not pretend it owns an IMMEDIATE transaction")
        with pytest.raises(RuntimeError, match="registration transaction"):
            store.allocate_employee_id(db)
        assert not store.path.with_name(store.path.name + ".employee-sequence").exists()
    with store.transaction() as db:
        db.execute("UPDATE employee_sequence SET next_number=3")
    with store.read() as db:
        assert value(db) == 3


@pytest.mark.parametrize("rollback", [False, True])
def test_nested_mutations_and_reads_share_outer_write_ownership(tmp_path, rollback):
    store = Store.scratch(tmp_path / "firm.sqlite3")
    trace = []
    try:
        with store.transaction() as outer:
            outer.set_trace_callback(trace.append)
            outer.execute("UPDATE employee_sequence SET next_number=2")
            with store.transaction() as nested:
                assert nested is outer
                nested.execute("UPDATE employee_sequence SET next_number=3")
            with store.read() as reader:
                assert reader is outer
                assert value(reader) == 3
            if rollback:
                raise ValueError("rollback")
    except ValueError:
        pass
    assert [sql for sql in trace if sql in {"COMMIT", "ROLLBACK"}] == [
        "ROLLBACK" if rollback else "COMMIT"
    ]
    with store.read() as db:
        assert value(db) == (1 if rollback else 3)


def test_write_scope_reserves_writer_before_first_mutation(tmp_path):
    store = Store.scratch(tmp_path / "firm.sqlite3")
    other = sqlite3.connect(store.path, timeout=0.01, isolation_level=None)
    try:
        with store.transaction():
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
    finally:
        other.close()
