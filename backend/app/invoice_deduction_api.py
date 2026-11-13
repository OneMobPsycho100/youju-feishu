"""管理员按公司主体维护发票抵扣规则。"""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, StringConstraints, field_validator
from sqlalchemy.orm import Session

from .auth import admin_user
from .db import begin_write, get_db
from .invoice_display import BILLING_TYPES
from .models import Company, InvoiceDeductionConfig, User
from .schemas import Schema
from .services import log

router = APIRouter(prefix="/api/v1/companies", tags=["主体管理"])


class InvoiceDeductionRule(Schema):
    """该主体在指定票种和费用类型下的抵扣方式。"""
    billing_type: str = Field(description="与已识别发票的细分票种精确匹配")
    expense_type: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)] = Field(
        description="与关联审批的费用类型精确匹配，忽略首尾空白")
    mode: Literal["FULL", "NONE"] = Field(description="FULL 按核对后的票面税额全额抵扣，NONE 明确不抵扣")

    @field_validator("billing_type")
    @classmethod
    def supported_billing_type(cls, value: str) -> str:
        if value not in BILLING_TYPES:
            raise ValueError("请选择已支持的细分票种")
        return value


class InvoiceDeductionRulesInput(Schema):
    expected_version: int = Field(ge=0, description="读取配置时取得的版本；首次保存为 0")
    rules: list[InvoiceDeductionRule] = Field(description="保存后的完整规则列表；空列表清空规则")

class InvoiceDeductionRulesOut(Schema):
    version: int = Field(description="当前配置版本；未保存时为 0")
    rules: list[InvoiceDeductionRule] = Field(description="当前主体的全部抵扣规则")
    billing_types: list[str] = Field(description="规则可选的细分票种")


def _rules_out(config: InvoiceDeductionConfig | None) -> InvoiceDeductionRulesOut:
    return InvoiceDeductionRulesOut(version=config.version if config else 0,
        rules=config.rules if config else [], billing_types=list(BILLING_TYPES))


@router.get("/{company_id}/invoice-deduction-rules", response_model=InvoiceDeductionRulesOut, summary="读取主体发票抵扣规则")
def get_rules(company_id: str, _user: User = Depends(admin_user), db: Session = Depends(get_db)):
    if not db.get(Company, company_id):
        raise HTTPException(404, "主体不存在")
    return _rules_out(db.get(InvoiceDeductionConfig, company_id))


@router.put("/{company_id}/invoice-deduction-rules", response_model=InvoiceDeductionRulesOut, summary="整组保存主体发票抵扣规则")
def save_rules(company_id: str, data: InvoiceDeductionRulesInput, user: User = Depends(admin_user),
               db: Session = Depends(get_db)):
    rule_keys = {(rule.billing_type, rule.expense_type) for rule in data.rules}
    if len(rule_keys) != len(data.rules):
        raise HTTPException(422, "同一票种和费用类型只能配置一条抵扣规则")
    begin_write(db)
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "主体不存在")
    config = db.get(InvoiceDeductionConfig, company_id)
    if data.expected_version != (config.version if config else 0):
        raise HTTPException(409, "抵扣规则已更新，请重新打开后编辑")
    if config is None:
        config = InvoiceDeductionConfig(company_id=company_id)
        db.add(config)
    config.rules = [rule.model_dump() for rule in data.rules]
    config.version = data.expected_version + 1
    log(db, user.name, "更新发票抵扣规则", company.name,
        {"company_id": company.id, "version": config.version, "rules": config.rules})
    db.commit()
    return _rules_out(config)
