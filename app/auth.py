"""基于静态令牌的角色鉴权（演示用）。

三个角色：
- inspector（检验员）：导入文件、提交报告
- reviewer（复核员）：处理冲突、批准/撤回报告、标记失效
- admin：参考数据维护与全量查询
令牌通过 Authorization: Bearer <token> 或 X-API-Token 传递。
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status

from app.config import get_settings

ROLE_PERMISSIONS = {
    "inspector": {"import", "submit", "read", "qc_observe", "qc_monitor"},
    "reviewer": {"review", "approve", "withdraw", "invalidate", "read",
                 "qc_monitor", "qc_clear"},
    "admin": {"seed", "read", "review", "import", "submit", "qc_configure"},
}


def _extract(authorization: str | None, x_api_token: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    if x_api_token:
        return x_api_token.strip()
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="缺少凭据：请提供 Authorization: Bearer <token> 或 X-API-Token",
    )


def current_role(
    authorization: str | None = Header(default=None),
    x_api_token: str | None = Header(default=None),
) -> str:
    token = _extract(authorization, x_api_token)
    settings = get_settings()
    for role, role_token in settings.role_tokens.items():
        if token == role_token:
            return role
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌无效")


def require(*permissions: str):
    def checker(role: str = Depends(current_role)) -> str:
        granted = ROLE_PERMISSIONS.get(role, set())
        missing = [p for p in permissions if p not in granted]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"角色 {role} 缺少权限：{', '.join(missing)}",
            )
        return role

    return checker
