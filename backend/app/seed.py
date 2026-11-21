"""种子数据：每个模块一条记录，精简格式。"""
from typing import Any

# 通用种子字段模板
def _seed(id: int, fields: dict[str, str]) -> dict[str, Any]:
    return {"id": id, "status": "草稿", "pending": True, "abnormal": False, **fields}


def _scan_field(field_id: str, name: str, physical: str = "") -> dict[str, str]:
    return {"field_id": field_id, "name": name, "physical_column": physical or name, "data_type": "string"}


# 字段变更对照的演示数据：来源 1 有 v1/v2 两版字段快照，语义模型 1 的
# order_amount 在 v2 里物理列改名为 amount_cents（字段标识未变，别名应沿用），
# 而 name 在 v2 中彻底消失（真正的缺列，需人工确认）。
_SEED_SCAN_FIELDS = [
    _scan_field("S1-C01", "id"),
    _scan_field("S1-C02", "user_id"),
    _scan_field("S1-C03", "order_amount"),
    _scan_field("S1-C04", "name"),
    _scan_field("S1-C05", "region"),
]
_SEED_SCAN_FIELDS_V2 = [
    _scan_field("S1-C01", "id"),
    _scan_field("S1-C02", "user_id"),
    _scan_field("S1-C03", "order_amount", "amount_cents"),
    _scan_field("S1-C05", "region"),
    _scan_field("S1-C06", "category", "category"),
]

SEED_ROWS: dict[str, list[dict[str, Any]]] = {
    "field_scan_batch": [
        {
            "id": 1, "scan_batch_id": "SCAN-0001-0001", "source_id": 1, "source_no": "SOUR-0001",
            "source_type": "mysql", "batch_no": "SEED-20261101", "version": 1,
            "fields": _SEED_SCAN_FIELDS, "field_count": len(_SEED_SCAN_FIELDS),
            "input_digest": "seed-scan-v1", "scanned_by": "auto_discover",
            "scanned_at": "2026-11-01 08:00:00", "status": "历史",
            "pending": False, "abnormal": False,
        },
        {
            "id": 2, "scan_batch_id": "SCAN-0001-0002", "source_id": 1, "source_no": "SOUR-0001",
            "source_type": "mysql", "batch_no": "SEED-20261115", "version": 2,
            "fields": _SEED_SCAN_FIELDS_V2, "field_count": len(_SEED_SCAN_FIELDS_V2),
            "input_digest": "seed-scan-v2", "scanned_by": "auto_discover",
            "scanned_at": "2026-11-15 08:00:00", "status": "生效",
            "pending": False, "abnormal": False,
        },
    ],
    "field_alias": [
        {
            "id": 1, "model_id": 1, "source_id": 1, "source_no": "SOUR-0001",
            "alias_version": 1, "field_id": "S1-C01", "alias": "id",
            "physical_column": "id", "status": "生效", "confirmed_by": "system",
            "note": "模型字段别名初始化", "created_at": "2026-11-01 08:00:00",
        },
        {
            "id": 2, "model_id": 1, "source_id": 1, "source_no": "SOUR-0001",
            "alias_version": 1, "field_id": "S1-C04", "alias": "name",
            "physical_column": "name", "status": "生效", "confirmed_by": "system",
            "note": "模型字段别名初始化", "created_at": "2026-11-01 08:00:00",
        },
        {
            "id": 3, "model_id": 1, "source_id": 1, "source_no": "SOUR-0001",
            "alias_version": 1, "field_id": "S1-C03", "alias": "order_amount",
            "physical_column": "order_amount", "status": "生效", "confirmed_by": "system",
            "note": "模型字段别名初始化", "created_at": "2026-11-01 08:00:00",
        },
    ],
    "source_registry": [_seed(1, {"源编号":"SOUR-0001","源类型":"mysql","连接地址":"db://demo","驱动配置":"mysql_driver","数据库名":"demo_db","认证方式":"token","接入状态":"待验证","最近同步":"2026-11-01"})],
    "semantic_model": [_seed(1, {"模型编号":"SEMA-0001","模型名称":"语义模型样例","源表映射":"source_registry:1","字段别名":"id,name,order_amount","度量定义":"COUNT(*),SUM(order_amount),COUNT(name)","维度定义":"region,category","计算口径":"标准口径","版本序号":"v1.0"})],
    "query_federation": [_seed(1, {"计划编号":"QUER-0001","参与来源":"SOUR-0001","关联键":"id","下推策略":"FULL_PUSHDOWN","数据量估算":"1000","并行度":"4","超时上限":"30s","结果合并":"DIRECT"})],
    "olap_cube": [_seed(1, {"立方编号":"OLAP-0001","立方名称":"多维立方样例","维度列表":"region,category","度量列表":"revenue,orders","聚合类型":"SUM","分区策略":"dt","刷新周期":"3600","数据行数":"0"})],
    "metric_factory": [_seed(1, {"指标编号":"METR-0001","指标名称":"指标工厂样例","数据来源":"olap_cube:1","计算公式":"SUM(revenue)","聚合窗口":"daily","精度模式":"exact","依赖立方":"1","最后产出":"2026-11-01"})],
    "dimension_tree": [_seed(1, {"层级编号":"DIME-0001","维度名称":"维度树样例","层级路径":"region>city","叶子字段":"city","汇总规则":"SUM","缓慢变化策略":"SCD2","行数预估":"1000","版本标签":"v1"})],
    "aggregate_cache": [_seed(1, {"配置编号":"AGGR-0001","目标立方":"olap_cube:1","缓存粒度":"2维x2度","TTL秒数":"3600","预热策略":"LAZY","命中阈值":"0.8","淘汰算法":"LRU","缓存大小":"1024"})],
    "query_rewrite": [_seed(1, {"规则编号":"QUER-0001","原始模式":"SELECT * FROM","替换模式":"SELECT /*optimized*/ FROM","适用模型":"semantic_model:1","改写条件":"always","代价阈值":"1000","命中次数":"0","最后命中":"-"})],
    "time_window": [_seed(1, {"窗口编号":"TIME-0001","窗口类型":"滑动","滑动宽度":"3600","滑动步长":"600","对齐基准":"epoch","缺值策略":"fill_prev","窗口别名":"1h_window","最后计算":"-"})],
    "dataset_slice": [_seed(1, {"切片编号":"DATA-0001","源立方":"1","维度过筛":"region=CN","度量过滤":"revenue>0","切片标签":"active","行数统计":"500","创建人":"admin","最后刷新":"-"})],
    "drill_config": [_seed(1, {"路径编号":"DRIL-0001","起始维度":"region","目标维度":"city","下钻类型":"hierarchy","关联键":"region_id","跳转规则":"exact","展示模板":"table","最后使用":"-"})],
    "result_mart": [_seed(1, {"集市编号":"RESU-0001","集市名称":"结果集市样例","数据来源":"result_cache","主题域":"analytics","行数统计":"0","刷新周期":"manual","下游消费":"dashboard","最后刷新":"-"})],
    "benchmark_set": [_seed(1, {"基线编号":"BENC-0001","基线名称":"基准集样例","数据范围":"2026-09","基线时段":"2026-09-01","对比维度":"region","指标数值":"1000","方差阈值":"0.1","锁定状态":"unlocked"})],
    "query_audit": [_seed(1, {"记录编号":"QUER-0001","查询语句":"SELECT * FROM demo","执行耗时":"100ms","扫描行数":"1000","返回行数":"10","数据来源":"query_federation","操作用户":"admin","查询时间":"2026-11-01"})],
    "cost_govern": [_seed(1, {"策略编号":"COST-0001","适用角色":"analyst","查询上限":"10000","并发上限":"10","月度配额":"100000","告警阈值":"80","限流动作":"monitor","生效时段":"2026-11"})],
    "freshness_mon": [_seed(1, {"规则编号":"FRES-0001","目标数据集":"all","SLA秒数":"3600","检测频率":"hourly","延迟阈值":"600","告警级别":"WARNING","触发次数":"0","最后检查":"-"})],
    "data_exporter": [_seed(1, {"任务编号":"DATA-0001","数据来源":"result_cache","筛选条件":"all","输出格式":"csv","字段列表":"*","排序规则":"id","导出上限":"1000","接收地址":"local://"})],
    "schedule_job": [_seed(1, {"任务编号":"SCHE-0001","目标立方":"1","Cron表达式":"0 */1 * * *","时区设置":"Asia/Shanghai","依赖检查":"none","超时上限":"600","失败通知":"email","下次执行":"-"})],
    "data_lake_read": [_seed(1, {"路径编号":"DATA-0001","存储位置":"s3://data-lake/prod","文件格式":"parquet","分区模式":"dt","Schema推导":"","文件列表":"","最后扫描":"","数据量":""})],
    "result_cache": [_seed(1, {"条目编号":"RESU-0001","查询摘要":"sample query","结果哈希":"abc123","缓存命中":"5","过期时间":"2027-01-01","存储大小":"100","创建时间":"2026-11-01","最后命中":"2026-11-01"})],
    "data_contract": [_seed(1, {"契约编号":"DATA-0001","数据集":"analytics","Schema约束":"id,name,value","SLA定义":"每小时更新","更新频率":"3600","负责人":"admin","消费者":"dashboard","生效时间":"2026-11-01"})],
    "ab_config": [_seed(1, {"实验编号":"AB_C-0001","实验名称":"实验配置样例","分流比例":"50:50","指标集合":"revenue","对照组":"baseline","实验组":"variant","置信水平":"0.95","运行期间":"2026-11"})],
}