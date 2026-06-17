"""
Authentication routes for JWT-based user registration, login, and token management.

Provides:
- POST /api/auth/register  - Create a new account (+ default organization)
- POST /api/auth/login     - Authenticate and receive JWT tokens
- POST /api/auth/refresh   - Refresh an expired access token
- POST /api/auth/logout    - Logout (stateless no-op, returns success)
- GET  /api/auth/me        - Retrieve current user profile
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..database import Organization, OrgMember, User, UserProjectAccess
from ..database.engine import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["Auth"])

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: EmailStr
    name: str = Field(..., min_length=1, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    avatar_url: str | None
    role: str
    is_active: bool
    status: str  # "pending" | "active" — frontend gates access on this
    created_at: datetime

    class Config:
        from_attributes = True


class AuthResponse(BaseModel):
    user: UserResponse
    access_token: str
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str


class MessageResponse(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def _create_access_token(user: User) -> str:
    """Create a short-lived access token containing user claims."""
    settings = get_settings()
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {
        "sub": user.id,
        "email": user.email,
        "role": user.role,
        "type": "access",
        "exp": expires,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _create_refresh_token(user: User) -> str:
    """Create a long-lived refresh token containing only the user id."""
    settings = get_settings()
    expires = datetime.now(timezone.utc) + timedelta(
        days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS
    )
    payload = {
        "sub": user.id,
        "type": "refresh",
        "exp": expires,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _slugify(text: str) -> str:
    """Convert a string to a URL-friendly slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text


# ---------------------------------------------------------------------------
# Dependency: get current user from JWT in request.state
# ---------------------------------------------------------------------------


async def get_current_user(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User:
    """Dependency that extracts the authenticated user from request.state.

    The ``TokenAuthMiddleware`` populates ``request.state.user`` with
    the JWT payload when a valid JWT is present.  This dependency loads
    the full ``User`` ORM object from the database.
    """
    user_data = getattr(request.state, "user", None)
    if user_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = user_data.get("id") if isinstance(user_data, dict) else None
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


async def require_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Like ``get_current_user`` but rejects accounts awaiting approval.

    Apply to every route that exposes project data so a freshly-registered
    (``pending``) account cannot read anything until an admin approves it.
    ``/auth/me`` deliberately uses ``get_current_user`` instead, so a pending
    client can still load its own profile and render the waiting screen.
    """
    if current_user.status == "pending":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account is awaiting administrator approval",
        )
    return current_user


async def require_approver(
    current_user: User = Depends(require_active_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Authorize a user permitted to approve/reject pending sign-ups.

    Allowed when the user has the global ``admin`` role, or is an ``owner``/
    ``admin`` of any organization. Builds on ``require_active_user`` so a
    pending account (even one that owns its auto-created personal org) can
    never approve others.
    """
    if current_user.role == "admin":
        return current_user
    result = await db.execute(
        select(OrgMember).where(
            OrgMember.user_id == current_user.id,
            OrgMember.role.in_(["owner", "admin"]),
        )
    )
    if result.first() is not None:
        return current_user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Administrator privileges required",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new user account",
)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """Register a new user.

    Creates the user record, a default *Personal* organization, and adds
    the user as its owner.  Returns JWT access and refresh tokens.

    Access gate: the very first account bootstraps as an active admin (so
    there is someone who can approve others); every subsequent sign-up starts
    in the ``pending`` status and cannot see any projects until an admin
    approves it. Tokens are still issued so the client can poll ``/auth/me``
    and render the "awaiting approval" screen.
    """
    # Check for existing user
    result = await db.execute(select(User).where(User.email == body.email))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists",
        )

    user_count = (
        await db.execute(select(func.count()).select_from(User))
    ).scalar() or 0
    is_first_user = user_count == 0

    # Create user
    user = User(
        email=body.email,
        name=body.name,
        password_hash=pwd_context.hash(body.password),
        role="admin" if is_first_user else "user",
        status="active" if is_first_user else "pending",
    )
    db.add(user)
    await db.flush()  # Populate user.id before creating org

    # Create default organization
    slug = _slugify(body.name) + "-personal"
    # Ensure slug uniqueness by appending a short suffix if needed
    existing_slug = await db.execute(
        select(Organization).where(Organization.slug == slug)
    )
    if existing_slug.scalar_one_or_none() is not None:
        slug = f"{slug}-{user.id[:8]}"

    org = Organization(
        name="Personal",
        slug=slug,
        owner_id=user.id,
        plan="free",
    )
    db.add(org)
    await db.flush()

    # Add user as owner member
    membership = OrgMember(
        org_id=org.id,
        user_id=user.id,
        role="owner",
    )
    db.add(membership)

    await db.commit()
    await db.refresh(user)

    logger.info(
        f"New user registered: {user.email} (id={user.id}, status={user.status})"
    )

    return AuthResponse(
        user=UserResponse.model_validate(user),
        access_token=_create_access_token(user),
        refresh_token=_create_refresh_token(user),
    )


@router.post(
    "/login",
    response_model=AuthResponse,
    summary="Authenticate and receive JWT tokens",
)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Authenticate with email and password.

    Returns a short-lived access token (15 min) and a long-lived refresh
    token (7 days).
    """
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if user is None or not pwd_context.verify(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated",
        )

    logger.info(f"User logged in: {user.email}")

    return AuthResponse(
        user=UserResponse.model_validate(user),
        access_token=_create_access_token(user),
        refresh_token=_create_refresh_token(user),
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Refresh an expired access token",
)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    """Exchange a valid refresh token for a new access token.

    The refresh token itself is not rotated; it remains valid until its
    original expiry.
    """
    settings = get_settings()

    try:
        payload = jwt.decode(
            body.refresh_token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is not a refresh token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return TokenResponse(access_token=_create_access_token(user))


@router.post(
    "/logout",
    response_model=MessageResponse,
    summary="Logout (stateless)",
)
async def logout():
    """Logout endpoint.

    Since JWT tokens are stateless, this is a no-op on the server side.
    Clients should discard their stored tokens.
    """
    return MessageResponse(message="Successfully logged out")


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get current user profile",
)
async def me(current_user: User = Depends(get_current_user)):
    """Return the profile of the currently authenticated user."""
    return UserResponse.model_validate(current_user)


class MyAccessResponse(BaseModel):
    is_admin: bool
    # True when the user has no grants and therefore sees everything.
    unrestricted: bool
    # project_id -> list of allowed page ids, or None meaning "all pages".
    projects: dict[str, list[str] | None]


@router.get(
    "/my-access",
    response_model=MyAccessResponse,
    summary="Get the current user's project/page access grants",
)
async def my_access(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Tell the client which projects/pages to show this user.

    Admins and users with no grants are ``unrestricted`` (the client shows
    everything). Otherwise ``projects`` maps each granted project id to its
    allowed page ids (or ``null`` for all pages). This is advisory: the client
    filters its UI, the server does not yet hard-enforce per-project routes.
    """
    if current_user.role == "admin":
        return MyAccessResponse(is_admin=True, unrestricted=True, projects={})

    result = await db.execute(
        select(UserProjectAccess).where(
            UserProjectAccess.user_id == current_user.id
        )
    )
    rows = list(result.scalars().all())
    if not rows:
        return MyAccessResponse(is_admin=False, unrestricted=True, projects={})

    projects: dict[str, list[str] | None] = {}
    for row in rows:
        pages: list[str] | None = None
        if row.pages_json:
            try:
                pages = json.loads(row.pages_json)
            except (ValueError, TypeError):
                pages = None
        projects[row.project_id] = pages
    return MyAccessResponse(is_admin=False, unrestricted=False, projects=projects)


# ---------------------------------------------------------------------------
# Member approval (admin only)
# ---------------------------------------------------------------------------


@router.get(
    "/pending-users",
    response_model=list[UserResponse],
    summary="List accounts awaiting approval (admin only)",
)
async def list_pending_users(
    _approver: User = Depends(require_approver),
    db: AsyncSession = Depends(get_db),
):
    """Return all sign-ups still awaiting approval, oldest first."""
    result = await db.execute(
        select(User)
        .where(User.status == "pending", User.is_active.is_(True))
        .order_by(User.created_at.asc())
    )
    return [UserResponse.model_validate(u) for u in result.scalars().all()]


@router.post(
    "/users/{user_id}/approve",
    response_model=UserResponse,
    summary="Approve a pending account (admin only)",
)
async def approve_user(
    user_id: str,
    approver: User = Depends(require_approver),
    db: AsyncSession = Depends(get_db),
):
    """Mark a pending account active so it can access the shared workspace."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    if user.status != "active" or not user.is_active:
        user.status = "active"
        user.is_active = True
        await db.commit()
        await db.refresh(user)
        logger.info(f"User approved: {user.email} (by {approver.email})")
    return UserResponse.model_validate(user)


@router.post(
    "/users/{user_id}/reject",
    response_model=MessageResponse,
    summary="Reject / revoke an account (admin only)",
)
async def reject_user(
    user_id: str,
    approver: User = Depends(require_approver),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate an account so it can no longer log in.

    Soft reject (kept, not deleted) so the email stays reserved and the action
    is reversible via approve.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    if user.id == approver.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot reject your own account",
        )
    user.is_active = False
    await db.commit()
    logger.info(f"User rejected: {user.email} (by {approver.email})")
    return MessageResponse(message="User rejected")
