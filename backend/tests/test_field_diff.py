"""字段变更对照：版本化快照、差异明细与语义模型投产校验。"""
import threading

import pytest

from app.services import field_diff
from app.store import store
from app.services.semantic_model import SemanticModelService


@pytest.fixture()
def source_id():
    """一个只属于本用例的 mysql 来源，隔离并发与共享状态。"""
    rows = store.rows("source_registry")
    new_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    rows.append({
        "id": new_id,
        "源编号": f"SRC-FD-{new_id:04d}",
        "源类型": "mysql",
        "连接地址": f"db://fd/{new_id}",
        "驱动配置": "mysql_driver",
        "数据库名": "fd_db",
        "认证方式": "token",
        "接入状态": "已接入",
        "最近同步": "2026-11-01",
        "status": "已注册",
        "pending": False,
        "abnormal": False,
    })
    return new_id


def _new_model(source_id: int, aliases: list[str], measures: str = "SUM(order_amount)") -> dict:
    rows = store.rows("semantic_model")
    model_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    model = {
        "id": model_id,
        "模型编号": f"SEMA-FD-{model_id:04d}",
        "模型名称": f"字段对照用例-{model_id}",
        "源表映射": f"source_registry:{source_id}",
        "字段别名": ",".join(aliases),
        "度量定义": measures,
        "维度定义": "region",
        "计算口径": "测试口径",
        "版本序号": "v1.0",
        "status": "校验中",
        "pending": True,
        "abnormal": False,
    }
    rows.append(model)
    return model


def _fields(*names: str) -> list[dict]:
    # 显式给出稳定字段标识：物理列改名时标识不变。
    return [
        {"field_id": f"F{i:02d}", "name": name, "physical_column": name}
        for i, name in enumerate(names, start=1)
    ]


def test_concurrent_scan_same_batch_writes_one_effective_snapshot(source_id):
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(field_diff.scan_source(source_id, batch_no="B-001", fields=_fields("id", "region")))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 8
    ids = {result["scan"]["id"] for result in results}
    assert len(ids) == 1, "同一来源同一批次只应有一个快照 id"
    assert sum(1 for result in results if not result["deduplicated"]) == 1

    rows = [row for row in store.rows(field_diff.SCAN_TABLE)
            if row["source_id"] == source_id and row["batch_no"] == "B-001"]
    assert len(rows) == 1
    assert rows[0]["status"] == field_diff.SCAN_EFFECTIVE


def test_concurrent_distinct_batches_leave_exactly_one_effective(source_id):
    barrier = threading.Barrier(6)
    errors = []

    def worker(index: int):
        barrier.wait()
        try:
            field_diff.scan_source(
                source_id, batch_no=f"P-{index}", fields=_fields("id", f"col_{index}"),
            )
        except Exception as exc:  # pragma: no cover - 失败时把异常带回主线程
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    rows = [row for row in store.rows(field_diff.SCAN_TABLE) if row["source_id"] == source_id]
    assert len(rows) == 6
    assert sum(1 for row in rows if row["status"] == field_diff.SCAN_EFFECTIVE) == 1
    assert sorted(row["version"] for row in rows) == list(range(1, 7))


def test_rename_keeps_alias_and_does_not_merge_same_name_across_sources(source_id):
    # 来源 A（当前用例来源）与来源 B 都有 region 列。
    other_rows = store.rows("source_registry")
    other_id = max((int(row.get("id", 0)) for row in other_rows), default=0) + 1
    other_rows.append({
        "id": other_id, "源编号": f"SRC-FD-{other_id:04d}", "源类型": "postgresql",
        "连接地址": "db://fd/other", "驱动配置": "pg", "数据库名": "other",
        "认证方式": "token", "接入状态": "已接入", "最近同步": "2026-11-01",
        "status": "已注册", "pending": False, "abnormal": False,
    })

    field_diff.scan_source(source_id, batch_no="B-1", fields=_fields("id", "region", "order_amount"))
    field_diff.scan_source(other_id, batch_no="B-1", fields=_fields("id", "region", "user_id"))

    model = _new_model(source_id, ["id", "region", "order_amount"])

    # 来源 A：region 物理列改名，字段标识不变。
    renamed = _fields("id", "region", "order_amount")
    renamed[1]["physical_column"] = "region_code"
    field_diff.scan_source(source_id, batch_no="B-2", fields=renamed)

    created = field_diff.create_diff_batch(model["id"])
    assert created["ok"]
    entries = created["entries"]

    renamed_entries = [e for e in entries if e["change_type"] == field_diff.CHANGE_RENAMED]
    assert len(renamed_entries) == 1
    assert renamed_entries[0]["field_id"] == "F02"
    assert renamed_entries[0]["resolution"] == field_diff.AUTO_ALIAS_KEPT
    # 改名不是缺列，不应阻断。
    assert not any(e["change_type"] == field_diff.CHANGE_MISSING for e in entries)

    # 不同来源的同名列各自成条，不合并。
    id_entries = [e for e in entries if e["field_id"] == "F02"]
    assert {e["source_id"] for e in id_entries} == {source_id}


def test_true_missing_column_blocks_measure_and_keeps_historical_batches(source_id):
    field_diff.scan_source(source_id, batch_no="B-1", fields=_fields("id", "order_amount", "region"))
    model = _new_model(source_id, ["id", "order_amount", "region"], measures="SUM(order_amount)")

    # 新快照里 order_amount 彻底消失（标识 F02 不再出现）。
    field_diff.scan_source(
        source_id, batch_no="B-2",
        fields=[{"field_id": "F01", "name": "id"}, {"field_id": "F03", "name": "region"}],
    )

    started = field_diff.start_promotion(model["id"])
    assert started["ok"]
    validation = started["validation"]
    conflict_entries = [e for e in validation["entries"] if e["change_type"] == field_diff.CHANGE_REMOVED]
    assert any(e["field_id"] == "F02" for e in conflict_entries)

    pending = [e for e in validation["entries"] if e["resolution"] == field_diff.RESOLUTION_PENDING]
    assert any(e["field_id"] == "F02" for e in pending)

    finished = field_diff.finish_promotion(started["promotion"]["id"])
    assert finished["ok"] is False
    assert finished["promotion"]["result"] == "阻断"
    assert "order_amount" in finished["promotion"]["conclusion"]

    # 结论已回写到差异清单。
    batch = field_diff.get_diff_batch(started["promotion"]["diff_batch_id"])
    assert batch["last_validation_result"] == "阻断"
    assert all(entry["校验结论"] for entry in batch["entries"])

    # 待确认差异不能并入已确认批次。
    confirm_all = field_diff.confirm_diff_batch(batch["id"])
    assert confirm_all["ok"] is False
    assert confirm_all["pending_entries"]

    # 确认缺列后批次才可确认，且历史批次内容保持不变。
    first_batch_snapshot = field_diff.get_diff_batch(batch["id"])
    for entry in batch["entries"]:
        if entry["resolution"] == field_diff.RESOLUTION_PENDING:
            resolved = field_diff.confirm_diff_entry(
                entry["id"], field_diff.RESOLUTION_MISSING, reviewer="reviewer-a",
            )
            assert resolved["ok"]
    confirmed = field_diff.confirm_diff_batch(batch["id"], reviewer="reviewer-a")
    assert confirmed["ok"] and confirmed["batch"]["status"] == field_diff.DIFF_CONFIRMED

    # 已确认批次不可再改写。
    blocked_rewrite = field_diff.confirm_diff_entry(
        batch["entries"][0]["id"], field_diff.RESOLUTION_IGNORED,
    )
    assert blocked_rewrite["ok"] is False
    after = field_diff.get_diff_batch(batch["id"])
    assert after["status"] == field_diff.DIFF_CONFIRMED
    for before_entry, after_entry in zip(
        sorted(first_batch_snapshot["entries"], key=lambda e: e["id"]),
        sorted(after["entries"], key=lambda e: e["id"]),
    ):
        assert before_entry["change_type"] == after_entry["change_type"]
        assert before_entry["field_id"] == after_entry["field_id"]

    # 按字段标识能查回上一轮已确认的缺列，并带回当时的输入摘要。
    history = field_diff.lookup_confirmed_missing("F02", model_id=model["id"])
    assert history and history[0]["batch"]["id"] == batch["id"]
    assert history[0]["batch"]["input_digest"] == batch["input_digest"]


def test_alias_corrected_after_validation_reads_frozen_input(source_id):
    field_diff.scan_source(source_id, batch_no="B-1", fields=_fields("id", "order_amount", "region"))
    model = _new_model(source_id, ["id", "order_amount", "region"], measures="SUM(order_amount)")
    field_diff.scan_source(
        source_id, batch_no="B-2",
        fields=[{"field_id": "F01", "name": "id"}, {"field_id": "F03", "name": "region"}],
    )

    started = field_diff.start_promotion(model["id"])
    promotion_id = started["promotion"]["id"]
    pinned_alias_version = started["promotion"]["alias_version"]

    # 校验读完差异之后，复核人把 order_amount 的别名改指到同来源的另一个物理列。
    fixed = field_diff.save_alias_version(
        model["id"],
        [{
            "source_id": source_id,
            "field_id": "F03",
            "alias": "order_amount",
            "physical_column": "region",
        }],
        confirmed_by="reviewer-b", note="复核更正别名",
    )
    assert fixed["ok"]
    assert fixed["alias_version"] > pinned_alias_version

    finished = field_diff.finish_promotion(promotion_id)
    # 结论仍按当次冻结的输入写：v1 的别名指向已消失的物理列，故为阻断。
    assert finished["promotion"]["result"] == "阻断"
    assert finished["promotion"]["alias_version"] == pinned_alias_version

    requery = field_diff.get_promotion(promotion_id)
    assert requery["alias_version"] == pinned_alias_version
    assert requery["current_alias_version"] > pinned_alias_version
    assert requery["alias_changed_since_validation"] is True
    assert requery["can_repromote"] is True
    assert requery["pinned_measures"][0]["status"] == "阻断"
    assert requery["current_measures"][0]["status"] == "可投产"
    assert "需重新发起投产校验" in requery["summary"]

    # 本次投产仍能回到实际采用的扫描与映射。
    assert requery["scans"] and requery["alias_snapshot"]
    assert requery["pinned"]["diff_batch_id"] == started["promotion"]["diff_batch_id"]
    assert requery["previous_confirmed_missing"] == {} or isinstance(requery["previous_confirmed_missing"], dict)


def test_model_action_goes_through_field_diff_and_api_lookup(source_id):
    field_diff.scan_source(source_id, batch_no="B-1", fields=_fields("id", "order_amount", "region"))
    model = _new_model(source_id, ["id", "order_amount", "region"], measures="SUM(order_amount)")

    entry, message = SemanticModelService().run_action(model["id"], "投产模型")
    assert entry is not None
    assert model["status"] == "已投产", message
    promotions = field_diff.list_promotions(model["id"])
    assert promotions and promotions[0]["result"] == "通过"
    assert promotions[0]["diff_batch_id"]


def test_same_name_columns_in_two_sources_stay_separate(source_id):
    """不同来源的同名列不得合并成一条差异。"""
    rows = store.rows("source_registry")
    peer_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    rows.append({
        "id": peer_id, "源编号": f"SRC-FD-{peer_id:04d}", "源类型": "mysql",
        "连接地址": f"db://fd/peer-{peer_id}", "驱动配置": "mysql_driver",
        "数据库名": "peer", "认证方式": "token", "接入状态": "已接入",
        "最近同步": "2026-11-01", "status": "已注册", "pending": False, "abnormal": False,
    })

    field_diff.scan_source(source_id, batch_no="M-1", fields=_fields("id", "revenue"))
    field_diff.scan_source(peer_id, batch_no="M-1", fields=_fields("id", "revenue"))

    model = {
        "id": max((int(row.get("id", 0)) for row in store.rows("semantic_model")), default=0) + 1,
        "模型编号": f"SEMA-MULTI-{peer_id}",
        "模型名称": f"多来源模型-{peer_id}",
        "源表映射": f"source_registry:{source_id},source_registry:{peer_id}",
        "字段别名": "id,revenue",
        "度量定义": "SUM(revenue)",
        "维度定义": "",
        "计算口径": "测试",
        "版本序号": "v1.0",
        "status": "校验中",
        "pending": True,
        "abnormal": False,
    }
    store.rows("semantic_model").append(model)

    # 两个来源都把 revenue 删掉（同时保留 id）：两条缺列差异分别挂在各自来源。
    field_diff.scan_source(source_id, batch_no="M-2", fields=_fields("id"))
    field_diff.scan_source(peer_id, batch_no="M-2", fields=_fields("id"))

    created = field_diff.create_diff_batch(model["id"])
    assert created["ok"]
    missing = [e for e in created["entries"] if e["change_type"] in (field_diff.CHANGE_MISSING, field_diff.CHANGE_REMOVED)]
    by_source = {}
    for entry in missing:
        by_source.setdefault(entry["source_id"], []).append(entry)
    assert set(by_source) == {source_id, peer_id}
    for entries in by_source.values():
        assert len(entries) == 1

    # 别名按来源各存一份，不跨来源合并。
    aliases = field_diff.list_alias_versions(model["id"])
    current = aliases["versions"][0]["entries"]
    assert {(row["source_id"], row["alias"]) for row in current} == {
        (source_id, "id"), (source_id, "revenue"),
        (peer_id, "id"), (peer_id, "revenue"),
    }


def test_api_field_diff_endpoints(source_id):
    """固定路径 /api/field-diff 不被通用模块路由抢先匹配。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    client.post("/api/field-diff/scans", json={"source_id": source_id, "batch_no": "API-1"})
    assert client.get("/api/field-diff/summary").status_code == 200
    assert client.get(f"/api/field-diff/scans?source_id={source_id}").json()["total"] == 1

    model = _new_model(source_id, ["id", "order_amount", "region"], measures="SUM(order_amount)")
    created = client.post("/api/field-diff/diffs", json={"model_id": model["id"]})
    assert created.status_code == 200, created.text
    batch_id = created.json()["batch"]["id"]
    assert client.get(f"/api/field-diff/diffs/{batch_id}").status_code == 200

    started = client.post(f"/api/field-diff/models/{model['id']}/promotions", json={})
    assert started.status_code == 200, started.text
    promotion_id = started.json()["promotion"]["id"]
    client.post(f"/api/field-diff/promotions/{promotion_id}/finish", json={})
    detail = client.get(f"/api/field-diff/promotions/{promotion_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["ok"] is True

    # 固定字面路径不会被 /api/{module}/{entry_id} 当成业务模块。
    assert client.get("/api/field-diff/confirmed-missing?field_id=ID-X").status_code == 200
