"""
Magestic AI Web Server - FastAPI Application.

Main entry point for the web server that provides:
- REST API for project/task management
- WebSocket endpoints for real-time streaming
- Static file serving for the React SPA
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles

from .auth import TokenAuthMiddleware
from .config import get_settings
from .database.engine import init_db
from .logging_config import setup_logging
from .services.skills_service import init_skills_service
from .routes import (
    api_keys,
    audit,
    auth_routes,
    context,
    email,
    execution,
    files,
    git,
    github,
    notifications,
    organizations,
    projects,
    skills,
    tasks,
    terminal,
    usage,
)
from .routes import logs as logs_routes
from .routes import cli_accounts as cli_accounts_routes
from .routes import llm_endpoints as llm_endpoints_routes
from .routes import settings as settings_routes
from .websockets import events as events_ws
from .websockets import logs as logs_ws
from .websockets import progress as progress_ws
from .websockets import terminal as terminal_ws

# Configure logging with file output
settings = get_settings()
setup_logging(log_level="DEBUG" if settings.DEBUG else "INFO")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup/shutdown."""
    settings = get_settings()

    # Startup
    logger.info("Starting Magestic AI Web Server...")
    logger.info(f"Backend path: {settings.BACKEND_PATH}")
    logger.info(f"Projects data dir: {settings.PROJECTS_DATA_DIR}")

    # Ensure data directory exists
    Path(settings.PROJECTS_DATA_DIR).mkdir(parents=True, exist_ok=True)

    # Auto-configure autoBuildPath if the backend directory exists
    # (enables project initialization without manual settings configuration)
    backend_path = Path(settings.BACKEND_PATH)
    if backend_path.exists():
        from .routes.settings import load_app_settings, save_app_settings
        app_settings = load_app_settings()
        if not app_settings.autoBuildPath:
            app_settings.autoBuildPath = str(backend_path)
            save_app_settings(app_settings)
            logger.info(f"Auto-configured autoBuildPath: {backend_path}")

    # Initialize database (creates tables if needed)
    await init_db()

    # Initialize skills service singleton once at startup
    init_skills_service()
    logger.info("SkillsService initialized")

    # Claude OAuth token: seed from mounted credentials file (or env vars)
    # and start the background refresh loop. Without this, agents 401 once
    # the initial access token expires (~1 hour).
    from .services.claude_token_service import get_claude_token_service
    token_service = get_claude_token_service()
    await token_service.start()
    logger.info("ClaudeTokenService started")

    yield

    # Shutdown
    logger.info("Shutting down Magestic AI Web Server...")
    await token_service.stop()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="Magestic AI Web API",
        description="Web API for Magestic AI autonomous coding framework",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.DEBUG else None,
        redoc_url="/redoc" if settings.DEBUG else None,
    )

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Add token auth middleware
    app.add_middleware(TokenAuthMiddleware)

    # Auth routes (prefix defined in router: /api/auth)
    app.include_router(auth_routes.router)

    # Organization routes (prefix defined in router: /api/orgs)
    app.include_router(organizations.router)

    # API key management (prefix defined in router: /api/keys)
    app.include_router(api_keys.router)

    # Audit log routes (prefix defined in router: /api/orgs)
    app.include_router(audit.router)

    # Notification routes (prefix defined in router: /api/notifications)
    app.include_router(notifications.router)

    # Routers exposing project data require an *approved* (non-pending) account.
    # A freshly self-registered user is "pending" and gets 403 here until an
    # admin approves them — this is the gate that stops new sign-ups from
    # seeing the shared workspace's projects.
    require_active = [Depends(auth_routes.require_active_user)]

    # Include API routers
    app.include_router(
        projects.router,
        prefix="/api/projects",
        tags=["Projects"],
        dependencies=require_active,
    )
    app.include_router(
        tasks.router,
        prefix="/api/tasks",
        tags=["Tasks"],
        dependencies=require_active,
    )
    app.include_router(usage.router, prefix="/api/usage", tags=["Usage"])
    # Execution routes also under /api/tasks for frontend compatibility
    app.include_router(
        execution.router,
        prefix="/api/tasks",
        tags=["Task Execution"],
        dependencies=require_active,
    )
    app.include_router(settings_routes.router, prefix="/api/settings", tags=["Settings"])
    app.include_router(cli_accounts_routes.router, prefix="/api/settings", tags=["CLI Accounts"])
    app.include_router(llm_endpoints_routes.router)
    app.include_router(files.router, prefix="/api/files", tags=["Files"])
    app.include_router(terminal.router, prefix="/api/terminals", tags=["Terminals"])

    # Email OAuth + account management routes (prefix defined in router: /api/email)
    app.include_router(email.router)

    # GitHub routes
    app.include_router(github.router, prefix="/api/github", tags=["GitHub"])

    # Git and utility routes
    app.include_router(git.router, prefix="/api/git", tags=["Git"])
    app.include_router(git.ollama_router, prefix="/api/ollama", tags=["Ollama"])
    app.include_router(git.claude_code_router, prefix="/api/claude-code", tags=["Claude Code"])
    app.include_router(git.mcp_router, prefix="/api/mcp", tags=["MCP"])
    app.include_router(git.updates_router, prefix="/api/updates", tags=["Updates"])

    # Memory infrastructure routes
    app.include_router(context.router, prefix="/api/memory", tags=["Memory"])

    # Logs viewing routes
    app.include_router(logs_routes.router, prefix="/api/logs", tags=["Logs"])

    # Skills knowledge base routes
    app.include_router(skills.router, prefix="/api/skills", tags=["Skills"])

    # Project-extension routes (SSH targets, DB profiles, transcripts)
    from .routes import extensions as extensions_routes
    app.include_router(
        extensions_routes.router,
        prefix="/api/ext",
        tags=["Extensions"],
        dependencies=require_active,
    )

    # Hermes chat router (LLM dispatch + grounded chat)
    from .routes import hermes as hermes_routes
    app.include_router(
        hermes_routes.router,
        prefix="/api/ext",
        tags=["Hermes"],
        dependencies=require_active,
    )

    # Project-docs ingest (multi-file upload → docs_index_service)
    from .routes import project_ingest as project_ingest_routes
    app.include_router(
        project_ingest_routes.router,
        prefix="/api/ext",
        tags=["Ingest"],
        dependencies=require_active,
    )

    # Transcript ingest (audio/video/transcript upload → graphify refresh)
    from .routes import transcripts as transcripts_routes
    app.include_router(
        transcripts_routes.router,
        prefix="/api/ext",
        tags=["Transcripts"],
        dependencies=require_active,
    )

    # MkDocs-based per-project documentation: agent generates markdown,
    # service runs `mkdocs build`, viewer serves the static site.
    from .routes import docs as docs_routes
    app.include_router(
        docs_routes.router,
        prefix="/api/projects",
        tags=["Docs"],
        dependencies=require_active,
    )

    # Include WebSocket routers
    app.include_router(logs_ws.router, tags=["WebSocket"])
    app.include_router(progress_ws.router, tags=["WebSocket"])
    app.include_router(terminal_ws.router, tags=["WebSocket"])
    app.include_router(events_ws.router, tags=["WebSocket"])

    # Health check endpoint (no auth required)
    @app.get("/api/health")
    async def health_check():
        return {"status": "healthy", "version": "1.0.0"}

    # Mount static files for SPA (if build directory exists)
    static_dir = Path(__file__).parent.parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
    else:
        # Placeholder for development
        @app.get("/")
        async def root():
            return {
                "message": "Magestic AI Web Server",
                "docs": "/docs",
                "note": "Frontend not built yet. Run 'npm run build' in apps/frontend-web/",
            }

    return app


# Create the app instance
app = create_app()


# Add Bearer token auth to OpenAPI schema so Swagger UI shows the Authorize button
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    openapi_schema.setdefault("components", {})["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "JWT access token or legacy API token",
        }
    }
    openapi_schema["security"] = [{"BearerAuth": []}]
    app.openapi_schema = openapi_schema
    return openapi_schema


app.openapi = custom_openapi


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()

    # Build uvicorn config
    uvicorn_config = {
        "app": "server.main:app",
        "host": settings.HOST,
        "port": settings.PORT,
        "reload": settings.DEBUG,
    }

    # Add SSL if enabled
    if settings.SSL_ENABLED:
        uvicorn_config["ssl_certfile"] = settings.SSL_CERTFILE
        uvicorn_config["ssl_keyfile"] = settings.SSL_KEYFILE
        logger.info(f"HTTPS enabled with certificate: {settings.SSL_CERTFILE}")

    uvicorn.run(**uvicorn_config)
