"""核心跨模块管线集成测试。"""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.store import store
from app.services.source_registry import SourceRegistryService
from app.services.semantic_model import SemanticModelService
from app.services.query_federation import QueryFederationService
from app.services.olap_cube import OlapCubeService
from app.services.data_exporter import DataExporterService
from app.services.result_cache import ResultCacheService
from app.services.data_lake_read import DataLakeReadService
from app.services.query_rewrite import QueryRewriteService
from app.services.query_audit import QueryAuditService
from app.services.cost_govern import CostGovernService
from app.services.data_contract import DataContractService

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_services():
    return {
        "source": SourceRegistryService(),
        "semantic": SemanticModelService(),
        "federation": QueryFederationService(),
        "cube": OlapCubeService(),
        "exporter": DataExporterService(),
        "result_cache": ResultCacheService(),
        "lake": DataLakeReadService(),
        "rewrite": QueryRewriteService(),
        "audit": QueryAuditService(),
        "cost": CostGovernService(),
        "contract": DataContractService(),
    }


class TestPipelineSourceToFederated:
    def test_register_source_triggers_schema_discovery(self, setup_services):
        """注册数据源后自动发现Schema、创建语义模型、写入审计和契约草案"""
        svc = setup_services["source"]
        entry, msg = svc.register_source({
            "源编号": "TEST-SOUR-001", "源类型": "mysql", "连接地址": "db://test/demo"})
        assert entry is not None, msg
        assert entry["接入状态"] == "已接入"

        models = store.rows("semantic_model")
        model = next(
            (m for m in models if f"source_registry:{entry['id']}" in str(m.get("源表映射", ""))),
            None)
        assert model is not None, "应自动创建语义模型"
        assert model["status"] == "已投产"

        contracts = store.rows("data_contract")
        contract = next(
            (c for c in contracts if "auto_" in str(c.get("数据集", ""))), None)
        assert contract is not None, "应自动创建数据契约草案"
        assert contract["status"] == "草稿"

    def test_api_source_to_federated(self):
        resp = client.post("/api/pipelines/source-to-federated",
                           params={"source_no": "SOUR-API-001", "source_type": "mysql",
                                   "source_addr": "db://api/demo"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] and data["execution"]["ok"]


class TestPipelineCubeBuildCache:
    def test_build_cube_computes_aggregates(self, setup_services):
        cube_svc = setup_services["cube"]
        entry, _ = cube_svc.create_entry({
            "立方编号": "TEST-CUBE-001", "立方名称": "测试立方",
            "维度列表": "region,category", "度量列表": "revenue,orders",
            "聚合类型": "SUM", "分区策略": "dt", "刷新周期": "3600", "数据行数": "0"})
        result = cube_svc.build_cube(entry["id"])
        assert result["ok"] and result["aggregated_rows"] > 0
        sample = result["sample"][0] if result["sample"] else {}
        assert "region" in sample or "category" in sample

        # 验证聚合缓存写入
        agg_caches = store.rows("aggregate_cache")
        cache = next(
            (c for c in agg_caches if f"olap_cube:{entry['id']}" in str(c.get("目标立方", ""))),
            None)
        assert cache is not None and cache["status"] == "生效中"

    def test_refresh_cube_evicts_old_cache(self, setup_services):
        cube_svc = setup_services["cube"]
        entry, _ = cube_svc.create_entry({
            "立方编号": "TEST-CUBE-003", "立方名称": "刷新测试",
            "维度列表": "region", "度量列表": "value",
            "聚合类型": "SUM", "分区策略": "dt", "刷新周期": "3600", "数据行数": "0"})
        cube_svc.build_cube(entry["id"])
        cube_id = entry["id"]
        before_counts = len([c for c in store.rows("result_cache") if str(cube_id) in str(c.get("查询摘要", ""))])
        result = cube_svc.refresh_cube(entry["id"])
        assert result["ok"]
        old_results = [c for c in store.rows("result_cache")
                       if c["status"] in ("已过期", "淘汰中", "已淘汰") and str(cube_id) in str(c.get("查询摘要", ""))]
        assert len(old_results) > 0


class TestPipelineLakeScanRegister:
    def test_scan_and_register(self, setup_services):
        svc = setup_services["lake"]
        entry, _ = svc.create_entry({
            "路径编号": "DATA-LAKE-001", "存储位置": "s3://data-lake/test",
            "文件格式": "parquet", "分区模式": "dt",
            "Schema推导": "", "文件列表": "", "最后扫描": "", "数据量": ""})
        result = svc.scan_and_register(entry["id"])
        assert result["ok"] and result["files_found"] > 0

        sources = store.rows("source_registry")
        lake_source = next(
            (s for s in sources if "lakehouse" in str(s.get("驱动配置", ""))), None)
        assert lake_source is not None

        models = store.rows("semantic_model")
        lake_model = next(
            (m for m in models if "湖仓自动发现" in str(m.get("模型名称", ""))), None)
        assert lake_model is not None


class TestDataConsistency:
    def test_cache_invalidation_on_data_change(self, setup_services):
        cache_svc = setup_services["result_cache"]
        source_svc = setup_services["source"]
        entry, _ = source_svc.register_source({
            "源编号": "TEST-CONSIST-001", "源类型": "mysql", "连接地址": "db://test"})
        result = cache_svc.invalidate_by_source(entry["id"])
        assert result["ok"]

    def test_freshness_check(self, setup_services):
        from app.services.freshness_mon import FreshnessMonService
        svc = FreshnessMonService()
        result = svc.check_data_freshness("test_dataset", 3600)
        assert result["ok"] and "breakdown" in result


class TestErrorHandling:
    def test_nonexistent_cube_build(self, setup_services):
        result = setup_services["cube"].build_cube(99999)
        assert not result["ok"]

    def test_missing_required_fields(self, setup_services):
        entry, missing = setup_services["source"].create_entry({"源编号": ""})
        assert entry is None and len(missing) > 0

    def test_empty_dimension_cube(self, setup_services):
        entry, missing = setup_services["cube"].create_entry({
            "立方编号": "TEST-BOUNDARY-001", "维度列表": "", "度量列表": ""})
        assert (entry is None and missing is not None) or not setup_services["cube"].build_cube(entry["id"])["ok"]