from dev_mcp.fleet.controller import wants_triple


def test_triple_only_when_owner_asks():
    assert wants_triple("архитектуру продумать минимум трижды независимо")
    assert not wants_triple("доделать compaction: цифры неверные, отключить автокомпацию")
