"""实验室原料检验数据治理服务。

模块划分：
- models:       PostgreSQL 表结构（采集原件 / 标准化结果 / 批准记录 / 质控监测）
- units:        单位量纲核对与换算
- parser:       CSV / JSON 原始记录解析（行级容错）
- processing:   原始记录 -> 标准化结果 -> 判定 的处理管线
- batch:        整批结论汇总
- reports:      报告批准 / 撤回 / 版本留痕（含质控影响限制检查）
- qc:           质控样漂移监测与受影响报告追查
- routers:      FastAPI 接口
"""
__version__ = "1.1.0"
