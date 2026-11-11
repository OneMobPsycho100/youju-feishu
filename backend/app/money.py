"""金额仅在数据库边界转换单位，避免 SQLite NUMERIC 隐式转为浮点数。"""
from decimal import Decimal

from sqlalchemy import BigInteger
from sqlalchemy.types import TypeDecorator


class Money(TypeDecorator):
    """数据库保存整数分，Python 返回两位小数 Decimal；HTTP 契约继续使用元。"""
    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        amount = Decimal(str(value))
        cents = amount * 100
        if not amount.is_finite() or cents != cents.to_integral_value() or abs(cents) > 999999999999999999:
            raise ValueError("金额必须是最多 18 位、包含两位小数的有效十进制数")
        return int(cents)

    def process_result_value(self, value, dialect):
        return (Decimal(value) / 100).quantize(Decimal("0.01")) if value is not None else None
