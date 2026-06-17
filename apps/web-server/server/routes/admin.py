"""
Admin routes — global user & access management (admin-only).

Exposes a management surface, separate from any single project, for a user with
the global ``admin`` role:

- list / create / update / delete user accounts
- set or generate a user's password ("recover password")
- grant a user access to specific projects and pages within them
- read the project list and the page (sidebar view) catalog used by the grant UI
- read / write the JIRA / Atlassian integration configuration (sync added later)

Access control here is *assignment + frontend filtering*: grants are persisted
and surfaced to the client (which hides ungranted projects/pages), but the
per-project data routes are not yet hardened server-side. See
``UserProjectAccess`` for the additive-grant semantics.
"""

import json
import logging
import secrets
import string
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import (
    IntegrationSetting,
    Organization,
    OrgMember,
    User,
    UserProjectAccess,
)
from ..database.engine import get_db
from .auth_routes import get_current_user, pwd_context, _slugify
from .projects import load_projects, project_to_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["Admin"])

# Pages (sidebar views) that can be granted per project. Keep the ids in sync
# with the frontend `SidebarView` / `PROJECT_VIEWS`. Global views (members,
# hermes, transcripts, admin) are intentionally excluded — they are not scoped
# to a project.
PAGE_CATALOG: list[dict[str, str]] = [
    {"id": "kanban", "label": "Tasks (Kanban)"},
    {"id": "editor", "label": "File Editor"},
    {"id": "insights", "label": "Chat / Insights"},
    {"id": "terminals", "label": "Terminals"},
    {"id": "agent-tools", "label": "Agent Tools"},
    {"id": "skills", "label": "Skills"},
    {"id": "docs", "label": "Documentation"},
    {"id": "changelog", "label": "Changelog"},
    {"id": "usage", "label": "Usage"},
    {"id": "worktrees", "label": "Worktrees"},
    {"id": "context", "label": "Context / Memory"},
    {"id": "github-issues", "label": "GitHub Issues"},
    {"id": "github-prs", "label": "GitHub PRs"},
]
_VALID_PAGE_IDS = {p["id"] for p in PAGE_CATALOG}

JIRA_INTEGRATION_KEY = "jira"


# ---------------------------------------------------------------------------
# Admin authorization
# ---------------------------------------------------------------------------


async def require_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Authorize a global administrator (``role == "admin"``).

    Stricter than ``require_approver``: org owners/admins do NOT qualify. This
    gates the whole admin surface, so it is intentionally limited to the global
    super-admin role. Bootstrap the first admin via ``make_admin.py`` or the DB.
    """
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required",
        )
    return current_user


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AdminUser(BaseModel):
    id: str
    email: str
    name: str
    role: str
    status: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class CreateUserRequest(BaseModel):
    email: EmailStr
    name: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)
    role: str = Field(default="user")  # "user" | "admin"


class UpdateUserRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    role: str | None = None  # "user" | "admin"
    status: str | None = None  # "pending" | "active"
    is_active: bool | None = None


class SetPasswordRequest(BaseModel):
    # When omitted, a strong random password is generated and returned once.
    password: str | None = Field(default=None, min_length=8, max_length=128)


class SetPasswordResponse(BaseModel):
    message: str
    # Populated only when the password was auto-generated, so the admin can
    # hand it to the user. Never stored or logged in plaintext.
    generated_password: str | None = None


class ProjectGrant(BaseModel):
    project_id: str
    # None / omitted => all pages for that project.
    pages: list[str] | None = None


class SetAccessRequest(BaseModel):
    grants: list[ProjectGrant]


class AccessResponse(BaseModel):
    user_id: str
    unrestricted: bool
    grants: list[ProjectGrant]


class JiraConfig(BaseModel):
    enabled: bool = False
    base_url: str | None = None  # e.g. https://your-domain.atlassian.net
    email: str | None = None  # Atlassian account email (API auth)
    api_token: str | None = None  # Atlassian API token
    project_key: str | None = None  # e.g. "ENG"
    jql: str | None = None  # optional filter


def _gen_password(length: int = 16) -> str:
    """Generate a readable strong random password."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get("/users", response_model=list[AdminUser])
async def list_users(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List every user account, newest first."""
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return [AdminUser.model_validate(u) for u in result.scalars().all()]


@router.post(
    "/users", response_model=AdminUser, status_code=status.HTTP_201_CREATED
)
async def create_user(
    body: CreateUserRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a new, already-approved (active) user plus a personal org.

    Mirrors self-registration but the admin sets the initial password and role,
    and the account is created ``active`` (no approval queue).
    """
    if body.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="role must be 'user' or 'admin'")

    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists",
        )

    user = User(
        email=body.email,
        name=body.name,
        password_hash=pwd_context.hash(body.password),
        role=body.role,
        status="active",
        is_active=True,
    )
    db.add(user)
    await db.flush()

    # Give the new user a personal organization (matches register()).
    slug = _slugify(body.name) + "-personal"
    dup = await db.execute(select(Organization).where(Organization.slug == slug))
    if dup.scalar_one_or_none() is not None:
        slug = f"{slug}-{user.id[:8]}"
    org = Organization(name="Personal", slug=slug, owner_id=user.id, plan="free")
    db.add(org)
    await db.flush()
    db.add(OrgMember(org_id=org.id, user_id=user.id, role="owner"))

    await db.commit()
    await db.refresh(user)
    logger.info(f"Admin {admin.email} created user {user.email} (role={user.role})")
    return AdminUser.model_validate(user)


@router.patch("/users/{user_id}", response_model=AdminUser)
async def update_user(
    user_id: str,
    body: UpdateUserRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update a user's name, role, status, or active flag."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    if body.role is not None:
        if body.role not in ("user", "admin"):
            raise HTTPException(status_code=400, detail="invalid role")
        if user.id == admin.id and body.role != "admin":
            raise HTTPException(
                status_code=400, detail="You cannot remove your own admin role"
            )
        user.role = body.role
    if body.status is not None:
        if body.status not in ("pending", "active"):
            raise HTTPException(status_code=400, detail="invalid status")
        user.status = body.status
    if body.is_active is not None:
        if user.id == admin.id and not body.is_active:
            raise HTTPException(
                status_code=400, detail="You cannot deactivate your own account"
            )
        user.is_active = body.is_active
    if body.name is not None:
        user.name = body.name

    await db.commit()
    await db.refresh(user)
    logger.info(f"Admin {admin.email} updated user {user.email}")
    return AdminUser.model_validate(user)


@router.post("/users/{user_id}/password", response_model=SetPasswordResponse)
async def set_password(
    user_id: str,
    body: SetPasswordRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Set a user's password, or generate a random one when none is supplied.

    The generated password is returned exactly once in the response so the admin
    can pass it to the user; it is never persisted in plaintext or logged.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    generated: str | None = None
    new_password = body.password
    if not new_password:
        generated = _gen_password()
        new_password = generated

    user.password_hash = pwd_context.hash(new_password)
    await db.commit()
    logger.info(f"Admin {admin.email} reset password for {user.email}")
    return SetPasswordResponse(
        message="Password updated", generated_password=generated
    )


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a user account and its access grants.

    Org ownership/membership is left intact at the DB level; deletion is meant
    for accounts created in error. Use deactivate (PATCH is_active=false) to
    reversibly block an account instead.
    """
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete yourself")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    await db.execute(
        delete(UserProjectAccess).where(UserProjectAccess.user_id == user_id)
    )
    await db.delete(user)
    await db.commit()
    logger.info(f"Admin {admin.email} deleted user {user.email}")


# ---------------------------------------------------------------------------
# Access grants
# ---------------------------------------------------------------------------


def _grants_for_rows(rows: list[UserProjectAccess]) -> list[ProjectGrant]:
    grants: list[ProjectGrant] = []
    for row in rows:
        pages: list[str] | None = None
        if row.pages_json:
            try:
                pages = json.loads(row.pages_json)
            except (ValueError, TypeError):
                pages = None
        grants.append(ProjectGrant(project_id=row.project_id, pages=pages))
    return grants


@router.get("/users/{user_id}/access", response_model=AccessResponse)
async def get_user_access(
    user_id: str,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Return a user's project/page grants. No grants => unrestricted."""
    result = await db.execute(
        select(UserProjectAccess).where(UserProjectAccess.user_id == user_id)
    )
    rows = list(result.scalars().all())
    return AccessResponse(
        user_id=user_id, unrestricted=len(rows) == 0, grants=_grants_for_rows(rows)
    )


@router.put("/users/{user_id}/access", response_model=AccessResponse)
async def set_user_access(
    user_id: str,
    body: SetAccessRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Replace a user's grants wholesale.

    An empty ``grants`` list clears all rows, returning the user to the
    unrestricted default. Admins are always unrestricted regardless of rows.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Validate page ids against the catalog.
    for g in body.grants:
        if g.pages is not None:
            bad = [p for p in g.pages if p not in _VALID_PAGE_IDS]
            if bad:
                raise HTTPException(
                    status_code=400, detail=f"Unknown page ids: {bad}"
                )

    # Replace: clear then re-insert (deduping by project_id, last wins).
    await db.execute(
        delete(UserProjectAccess).where(UserProjectAccess.user_id == user_id)
    )
    seen: set[str] = set()
    for g in body.grants:
        if g.project_id in seen:
            continue
        seen.add(g.project_id)
        db.add(
            UserProjectAccess(
                user_id=user_id,
                project_id=g.project_id,
                pages_json=json.dumps(g.pages) if g.pages is not None else None,
            )
        )
    await db.commit()
    logger.info(
        f"Admin {admin.email} set access for {user.email}: {len(seen)} project(s)"
    )

    result = await db.execute(
        select(UserProjectAccess).where(UserProjectAccess.user_id == user_id)
    )
    rows = list(result.scalars().all())
    return AccessResponse(
        user_id=user_id, unrestricted=len(rows) == 0, grants=_grants_for_rows(rows)
    )


# ---------------------------------------------------------------------------
# Catalog helpers (projects + pages) used by the grant UI
# ---------------------------------------------------------------------------


@router.get("/projects")
async def list_all_projects(_admin: User = Depends(require_admin)):
    """List every registered project (id, name, path) for the grant matrix."""
    projects = load_projects()
    return [
        {
            "id": pid,
            "name": pdata.get("name") or project_to_response(pid, pdata)["name"],
            "path": pdata.get("path", ""),
        }
        for pid, pdata in projects.items()
    ]


@router.get("/pages")
async def list_pages(_admin: User = Depends(require_admin)):
    """Return the catalog of grantable pages (sidebar views)."""
    return PAGE_CATALOG


# ---------------------------------------------------------------------------
# Integrations — JIRA / Atlassian (configured now, sync wired up later)
# ---------------------------------------------------------------------------


@router.get("/integrations/jira", response_model=JiraConfig)
async def get_jira_config(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Read the stored JIRA configuration (api_token is masked)."""
    result = await db.execute(
        select(IntegrationSetting).where(
            IntegrationSetting.key == JIRA_INTEGRATION_KEY
        )
    )
    row = result.scalar_one_or_none()
    if row is None or not row.config_json:
        return JiraConfig()
    data = json.loads(row.config_json)
    cfg = JiraConfig(**{**data, "enabled": row.enabled})
    if cfg.api_token:
        cfg.api_token = "********"  # never echo the secret back
    return cfg


@router.put("/integrations/jira", response_model=JiraConfig)
async def save_jira_config(
    body: JiraConfig,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Persist the JIRA configuration.

    A masked api_token ("********") is treated as "unchanged" so re-saving the
    form without re-typing the secret keeps the stored value.
    """
    result = await db.execute(
        select(IntegrationSetting).where(
            IntegrationSetting.key == JIRA_INTEGRATION_KEY
        )
    )
    row = result.scalar_one_or_none()
    existing = json.loads(row.config_json) if (row and row.config_json) else {}

    new = body.model_dump()
    if not new.get("api_token") or new.get("api_token") == "********":
        new["api_token"] = existing.get("api_token")
    enabled = bool(new.pop("enabled", False))

    if row is None:
        row = IntegrationSetting(key=JIRA_INTEGRATION_KEY)
        db.add(row)
    row.enabled = enabled
    row.config_json = json.dumps(new)
    row.updated_by = admin.id
    await db.commit()
    logger.info(f"Admin {admin.email} saved JIRA integration (enabled={enabled})")

    out = JiraConfig(**{**new, "enabled": enabled})
    if out.api_token:
        out.api_token = "********"
    return out
