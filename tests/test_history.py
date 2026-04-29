from pathlib import Path

from clopsctl.history import init_db, record, search


def test_record_and_search(tmp_path: Path):
    db = tmp_path / "h.sqlite"
    init_db(db)
    rid = record(db, server="web-1", mode="exec", command="ls /var/log", exit_code=0, stdout="syslog\n")
    assert rid > 0

    rows = search(db, server="web-1", limit=10)
    assert len(rows) == 1
    assert rows[0]["command"] == "ls /var/log"
    assert rows[0]["exit_code"] == 0


def test_grep(tmp_path: Path):
    db = tmp_path / "h.sqlite"
    record(db, server="web-1", mode="exec", command="df -h", stdout="80%")
    record(db, server="web-2", mode="exec", command="uptime", stdout="load 1.0")
    hits = search(db, grep="80%")
    assert len(hits) == 1
    assert hits[0]["server"] == "web-1"
