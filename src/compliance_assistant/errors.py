"""项目异常类型。

所有可预期的输入、数据库、规则、价格、校验和报告错误都继承
:class:`ComplianceError`，命令行入口可以据此返回非零退出码，同时保留
具体的错误类型供单元测试精确断言。
"""


class ComplianceError(Exception):
    """本项目所有可预期错误的基类。"""


class InputDataError(ComplianceError):
    """输入数据不符合约定。"""


class CsvLoadError(InputDataError):
    """CSV 读取或校验失败。"""


class DatabaseLoadError(ComplianceError):
    """数据库无法只读打开或查询失败。"""


class DatabaseSchemaError(DatabaseLoadError):
    """数据库缺少必需的表或字段，或表结构不符合约定。"""


class DatabaseDataError(DatabaseLoadError):
    """数据库中的业务数据非法。"""


class RuleConfigurationError(ComplianceError):
    """规则配置非法，无法进行确定性判定。"""


class PricingError(ComplianceError):
    """价格或汇率非法，无法换算。"""


class ProcessingError(ComplianceError):
    """单条 SKU 在处理阶段失败。"""


class ValidationError(ComplianceError):
    """处理结果未通过完整性校验。"""


class ReportError(ComplianceError):
    """报告生成、写入或复核失败。"""
