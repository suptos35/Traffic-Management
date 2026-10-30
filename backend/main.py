from contextlib import asynccontextmanager
import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn
from backend.adapters.http.routes import router
from backend.adapters.persistence.repository import SQLiteRepository
from backend.application.manager import JunctionManager
from backend.config.config import AppConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("TrafficSystem")

import sys

# Load configuration and repository
config_path = Path("config/default_config.yaml")
for i, arg in enumerate(sys.argv):
    if arg in ("--config", "-c") and i + 1 < len(sys.argv):
        config_path = Path(sys.argv[i + 1])

if not config_path.exists():
    config_path = Path(__file__).resolve().parents[1] / "config" / "default_config.yaml"

logger.info(f"Loading configuration from: {config_path}")
app_config = AppConfig.load_from_yaml(config_path)
repository = SQLiteRepository(db_path=app_config.persistence.db_path)
app_manager = JunctionManager(app_config, repository)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing Junction Manager and active junction workers...")
    await app_manager.initialize()
    logger.info(f"Traffic system initialized with junctions: {app_manager.list_junction_ids()}")
    yield
    logger.info("Shutting down Junction Manager...")
    await app_manager.shutdown()
    logger.info("Traffic system shutdown complete")


app = FastAPI(
    title="Factory Traffic Management System",
    description="Event-driven traffic signal control system for garment manufacturing facility (CSI Smart Tech Assessment V2)",
    version="2.0.0",
    lifespan=lifespan,
)

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Spec Section 10 & 19: Malformed request bodies return 400 Bad Request instead of FastAPI default 422."""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": "INVALID_REQUEST", "detail": exc.errors()},
    )

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if "api" not in request.url.path:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Include API router
app.include_router(router)

# Mount frontend static directory if exists
frontend_dir = Path("frontend")
if not frontend_dir.exists():
    frontend_dir = Path(__file__).resolve().parents[1] / "frontend"

if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")


if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host=app_config.api.host,
        port=app_config.api.port,
        reload=False,
    )
