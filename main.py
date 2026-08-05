"""
avry-payments Microservice Entry Point
Description: Payment processing, wallet management
"""

import logging
import os
import sys

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Load environment variables
load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

from app.config import settings  # noqa: E402  (must follow load_dotenv)

# Refuse to boot on a Midtrans configuration that cannot transact. A service
# that starts "successfully" but 401s against the gateway on every real payment
# is worse than one that fails loudly right here.
try:
    settings.validate_midtrans_config()
except ValueError as e:
    logger.error("Midtrans configuration invalid: %s", e)
    sys.exit(1)

# Create FastAPI app
app = FastAPI(
    title="AVRY Payments Service",
    description="Payment processing, wallet management",
    version="1.0.0"
)

# CORS restricted to first-party origins: "*" cannot legally be combined with
# allow_credentials, and a payment API should not be callable from anywhere.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With", "Accept"],
)

# Import and include routes. A payments service that silently starts without its
# payment routes would answer health checks while dropping every purchase, so an
# import failure is fatal.
try:
    from app.routes.payment import router as payment_router
    from app.routes.reporting import router as reporting_router
    from app.routes.wallet import router as wallet_router

    app.include_router(payment_router)
    app.include_router(wallet_router)
    app.include_router(reporting_router)
    logger.info("Payment, wallet and reporting routes registered")
except Exception as e:
    logger.error("Could not import payment routes: %s", e)
    raise


@app.on_event("startup")
async def warm_fx_rate():
    """
    Fetch the USD/IDR rate before serving, so the first customer isn't quoted
    from the static fallback, then start the background poller.

    Both are non-fatal: fx degrades to its cache/static fallback, and the service
    must still take payments if the rate provider is down.
    """
    import asyncio

    from app.services import fx

    await fx.ensure_fresh()
    logger.info("FX ready: %s", fx.rate_info())

    # Held on app.state so the task isn't garbage-collected mid-flight.
    app.state.fx_poller = asyncio.create_task(fx.run_poller())


@app.on_event("shutdown")
async def stop_fx_poller():
    """Cancel the poller so shutdown isn't held up by its sleep."""
    task = getattr(app.state, "fx_poller", None)
    if task:
        task.cancel()


# Health check endpoint
@app.get("/health")
async def health():
    """Service health check"""
    from app.services import fx
    from app.services.payment_gateway import midtrans_service

    return {
        "status": "healthy",
        "service": "avry-payments",
        "version": "1.0.0",
        "midtrans": {
            "configured": midtrans_service.is_configured(),
            "mode": "mock" if midtrans_service.mock_mode
                    else ("production" if midtrans_service.is_production else "sandbox"),
        },
        "fx": fx.rate_info(),
    }


@app.get("/")
async def root():
    """Service info"""
    return {
        "service": "AVRY Payments Service",
        "version": "1.0.0"
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "3030"))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        reload=False
    )
