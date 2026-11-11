"""管理员模板读取、迁移预览和原子导入接口。预览不会订阅或修改业务数据。"""
from fastapi import APIRouter, Depends
from fastapi.responses import Response

from . import template_schemas as t
from .auth import admin_user
from .db import get_db
from .models import now
from .template_transfer import export_bundle, import_bundle, preview_bundle
from .template_matching import inspect_template

router = APIRouter(prefix="/api/v1/templates", tags=["模板迁移"])


@router.post("/inspect", response_model=t.TemplateInspectOut, summary="读取飞书模板链接或编码，展开控件与审批节点")
def inspect_definition(data: t.TemplateInspectInput, user=Depends(admin_user), db=Depends(get_db)):
    """读取后自动识别业务字段和专用预审节点；已有配置或真实待办可提供当前应用审批人。"""
    return inspect_template(db, data)


@router.post("/export", summary="下载单个或批量审批模板配置 JSON")
def export_templates(data: t.TemplateExportInput, user=Depends(admin_user), db=Depends(get_db)):
    """白名单导出配置，不序列化 ORM 对象中的运行状态、身份或凭据。"""
    bundle = export_bundle(db, data.codes)
    return Response(bundle.model_dump_json(indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="approval-templates-{now():%Y%m%d-%H%M%S}.json"'})


@router.post("/import/preview", response_model=t.TemplatePreviewOut, summary="校验迁移文件并预览目标模板匹配")
def preview_templates(data: t.TemplatePreviewInput, user=Depends(admin_user), db=Depends(get_db)):
    return preview_bundle(db, data)


@router.post("/import", response_model=t.TemplateImportOut, summary="重新核对目标定义并原子导入已确认的配置")
def import_templates(data: t.TemplatePreviewInput, user=Depends(admin_user), db=Depends(get_db)):
    """重新读取飞书定义，检查预览摘要与配置冲突，全部校验通过才进入写事务。"""
    return import_bundle(db, data, user.name)
