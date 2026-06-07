"""
AVRY-Payments Service
Payment Processing and Wallet Management
Port: 3030
"""

import os
import sys
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from app.config import settings
from app.database.db_service import DatabaseService
from app.llm.ollama_client import OllamaClient

# Import routes
try:
    from app.routes.payment import router as payment_router
    from app.routes.wallet import router as wallet_router
    print("[✓] Payment and wallet routes imported")
except Exception as e:
    print(f"[!] Warning: Could not import routes: {e}")
    payment_router = None
    wallet_router = None

db_service = DatabaseService(base_path="data")
try:
    llm_client = OllamaClient(base_url=settings.ollama_base_url)
except Exception as e:
    print(f"[WARNING] LLM client: {e}")
    llm_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[{datetime.now().isoformat()}] [STARTUP] AVRY-Payments service starting on port 3030...")
    yield
    print(f"[{datetime.now().isoformat()}] [SHUTDOWN] AVRY-Payments service shutting down...")

app = FastAPI(
    title="AVRY Payments Service",
    version="1.0.0",
    description="Payment Processing and Wallet Management",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routes
if payment_router:
    app.include_router(payment_router)
if wallet_router:
    app.include_router(wallet_router)

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "avry-payments",
        "port": 3030,
        "timestamp": datetime.utcnow().isoformat()
    }

# ===== DEBUG =====

@app.get("/api/debug/info")
async def debug_info():
    return {
        "service": "avry-payments",
        "port": 3030,
        "version": "1.0.0",
        "timestamp": datetime.utcnow().isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", settings.port or 3030))
    print(f"\n[*] Starting AVRY-Payments on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False, log_level="info")
