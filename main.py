"""
avry-payments Microservice Entry Point
Description: Payment processing, wallet management
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Create FastAPI app
app = FastAPI(
    title="AVRY Payments Service",
    description="Payment processing, wallet management",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Import and include routes
try:
    from app.routes.payment import router as payment_router
    from app.routes.wallet import router as wallet_router
    app.include_router(payment_router)
    app.include_router(wallet_router)
    print("[✓] Payment and wallet routes registered")
except Exception as e:
    print(f"[!] Warning: Could not import payment routes: {e}")
try:
    from app.routes.payment import router as payment_router
    from app.routes.wallet import router as wallet_router
    
    app.include_router(payment_router)
    app.include_router(wallet_router)
except Exception as e:
    print(f"Warning: Could not import routes: {e}")

# Health check endpoint
@app.get("/health")
async def health():
    """Service health check"""
    return {
        "status": "healthy",
        "service": "avry-payments",
        "version": "1.0.0"
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
