import uuid
import logging
from fastapi import APIRouter, Depends, Request, HTTPException, status, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.deps import get_current_active_user, require_admin, get_db, CurrentUser, AdminUser, DB, get_space_access
from app.models.space import Space
from app.models.group import group_spaces
from app.schemas.space import SpaceCreate, SpaceRead, SpaceDetail

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/spaces", tags=["spaces"])
from app.templates_setup import templates


@router.get("", response_class=HTMLResponse)
async def list_spaces(
    request: Request,
    user: CurrentUser,
    db: DB,
):
    if user.is_admin:
        result = await db.execute(select(Space))
        spaces = result.scalars().all()
    else:
        user_group_ids = [g.id for g in user.groups]
        if user_group_ids:
            result = await db.execute(
                select(Space)
                .join(group_spaces, group_spaces.c.space_id == Space.id)
                .where(group_spaces.c.group_id.in_(user_group_ids))
                .distinct()
            )
            spaces = result.scalars().all()
        else:
            spaces = []

    return templates.TemplateResponse(
        request,
        "spaces/list.html",
        {"user": user, "spaces": spaces},
    )


@router.get("/{space_id}", response_class=HTMLResponse)
async def space_detail(
    space_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    db: DB,
):
    space = await get_space_access(space_id, user, db)

    return templates.TemplateResponse(
        request,
        "spaces/detail.html",
        {
"user": user,
            "space": space,
            "docker_hosts": space.docker_hosts,
            "notification_channels": space.notification_channels,
        },
    )


@router.get("/{space_id}/edit", response_class=HTMLResponse)
async def edit_space_page(
    space_id: uuid.UUID,
    request: Request,
    user: AdminUser,
    db: DB,
):
    result = await db.execute(select(Space).where(Space.id == space_id))
    space = result.scalar_one_or_none()
    if not space:
        raise HTTPException(status_code=404, detail="Space not found")
    return templates.TemplateResponse(
        request,
        "spaces/edit.html",
        {"user": user, "space": space},
    )


@router.post("/{space_id}/edit")
async def update_space(
    space_id: uuid.UUID,
    request: Request,
    user: AdminUser,
    db: DB,
    name: str = Form(...),
    description: str = Form(""),
    icon: str = Form(""),
):
    result = await db.execute(select(Space).where(Space.id == space_id))
    space = result.scalar_one_or_none()
    if not space:
        raise HTTPException(status_code=404, detail="Space not found")
    space.name = name.strip()
    space.description = description.strip() or None
    space.icon = icon.strip() or None
    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(db, "space.update", user=user, resource_type="space",
                 resource_id=space.id, resource_name=space.name, request=request)
    logger.info(f"Updated space: {space.name} by {user.email}")
    return RedirectResponse(url=f"/spaces/{space_id}", status_code=303)


@router.post("", response_class=RedirectResponse)
async def create_space(
    request: Request,
    user: AdminUser,
    db: DB,
    name: str = Form(...),
    description: str = Form(""),
    icon: str = Form(""),
):
    space = Space(name=name, description=description or None, icon=icon.strip() or None)
    db.add(space)
    await db.commit()
    await db.refresh(space)
    from app.services.audit_service import audit as _audit
    await _audit(db, "space.create", user=user, resource_type="space",
                 resource_id=space.id, resource_name=space.name, request=request)
    logger.info(f"Created space: {space.name} by {user.email}")
    return RedirectResponse(url=f"/spaces/{space.id}", status_code=302)


@router.delete("/{space_id}")
async def delete_space(
    space_id: uuid.UUID,
    request: Request,
    user: AdminUser,
    db: DB,
):
    result = await db.execute(select(Space).where(Space.id == space_id))
    space = result.scalar_one_or_none()
    if not space:
        raise HTTPException(status_code=404, detail="Space not found")
    sname = space.name
    await db.delete(space)
    await db.commit()
    from app.services.audit_service import audit as _audit
    await _audit(db, "space.delete", user=user, resource_type="space",
                 resource_id=str(space_id), resource_name=sname, request=request)
    await db.commit()
    logger.info(f"Deleted space: {sname} by {user.email}")
    return {"message": "Space deleted"}
