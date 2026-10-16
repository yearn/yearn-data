import pytest

from yearn_data.coverage import Scope, commit_range, insert_checked, missing_ranges
from yearn_data.storage import connect, init_db


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "coverage.sqlite")
    init_db(conn)
    yield conn
    conn.close()


def complete(db, lo, hi, scope=Scope(1, "vault", "reports"), write=lambda conn: 0):
    return commit_range(db, scope, lo, hi, source="rpc", end_hash="hash", run_id=None, write=write)


def test_union_and_interior_gaps_survive_changed_chunk_size(db):
    complete(db, 10, 20)
    complete(db, 15, 30)
    complete(db, 41, 45)
    assert list(missing_ranges(db, Scope(1, "VAULT", "reports"), 0, 50, 7)) == [
        (0, 6), (7, 9), (31, 37), (38, 40), (46, 50),
    ]


def test_successful_empty_scan_is_covered_but_not_another_scope(db):
    complete(db, 0, 100)
    assert list(missing_ranges(db, Scope(1, "vault", "reports"), 0, 100, 200)) == []
    for scope in [Scope(10, "vault", "reports"), Scope(1, "new", "reports"),
                  Scope(1, "vault", "flows"), Scope(1, "vault", "reports", "v2")]:
        assert list(missing_ranges(db, scope, 0, 100, 200)) == [(0, 100)]


def test_failed_writer_rolls_back_observations_and_coverage(db):
    def fail(conn):
        conn.execute("INSERT INTO index_state VALUES (1, 'vault', 'test', 100, 0)")
        raise RuntimeError("interrupted")
    with pytest.raises(RuntimeError):
        complete(db, 0, 100, write=fail)
    assert db.execute("SELECT count(*) FROM index_state").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM history_coverage").fetchone()[0] == 0
    complete(db, 0, 100)


def test_legacy_cursor_is_not_coverage(db):
    db.execute("INSERT INTO index_state VALUES (1, '*', 'StrategyReported:envio', 100, 0)")
    db.commit()
    assert list(missing_ranges(db, Scope(1, "vault", "reports"), 0, 100, 200)) == [(0, 100)]


def test_conflicting_identity_fails_atomically(db):
    values = dict(chain_id=1, target="vault", event_name="test", last_block=1, updated_at=0)
    with db:
        assert insert_checked(db, "index_state", values, ("chain_id", "target", "event_name")) == 1
        assert insert_checked(db, "index_state", values, ("chain_id", "target", "event_name")) == 0
    with pytest.raises(ValueError, match="conflicting"):
        complete(db, 0, 100, write=lambda conn: insert_checked(
            conn, "index_state", {**values, "last_block": 2}, ("chain_id", "target", "event_name")))
    assert db.execute("SELECT count(*) FROM history_coverage").fetchone()[0] == 0


@pytest.mark.parametrize("bounds", [(-1, 2, 1), (3, 2, 1), (0, 2, 0)])
def test_invalid_bounds(db, bounds):
    with pytest.raises(ValueError):
        list(missing_ranges(db, Scope(1, "vault", "reports"), *bounds))


def test_missing_metadata_can_be_enriched_but_financial_conflicts_cannot(db):
    values = dict(chain_id=1, contract_address='0xAb', event_name='test', tx_hash='0xAA', log_index=0,
                  block_number=1, block_timestamp=1, decoded_json='{"address":"0xAB","gain":1}')
    with db:
        insert_checked(db, 'events_raw', values, ('chain_id','tx_hash','log_index'))
        insert_checked(db, 'events_raw', {**values, 'decoded_json':'{"address":"0xab","gain":1}'}, ('chain_id','tx_hash','log_index'))
    with pytest.raises(ValueError, match='conflicting'):
        with db:
            insert_checked(db, 'events_raw', {**values, 'decoded_json':'{"address":"0xab","gain":2}'}, ('chain_id','tx_hash','log_index'))
