"""Configuration module for Aivory application"""
import os
import sys
from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import field_validator, ValidationError, ConfigDict
from dotenv import load_dotenv

# Load unified .env from project root (covers all services)
load_dotenv(".env.local")  # legacy — takes precedence if present
load_dotenv(".env")        # unified config


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""
    
    # Allow extra fields to be ignored (for shared .env across services)
    model_config = ConfigDict(extra='ignore')
    
    # Server configuration
    app_name: str = "Aivory AI Readiness Platform"
    app_version: str = "1.0.0"
    host: str = "0.0.0.0"
    port: int = 8081
    
    # LLM configuration (Ollama - legacy)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "mistralai/Mistral-7B-Instruct"
    llm_timeout: float = 5.0
    llm_max_tokens: int = 500
    llm_temperature: float = 0.7
    
    # OpenRouter AI configuration (PRIMARY)
    openrouter_api_key: Optional[str] = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    
    # Midtrans Payment Gateway configuration
    midtrans_server_key: Optional[str] = None
    midtrans_client_key: Optional[str] = None
    midtrans_is_production: bool = False
    # USD -> IDR conversion for Midtrans gross_amount. The live market rate is
    # fetched at runtime (see app/services/fx.py) and `fx_margin_percent` is
    # added on top; `usd_idr_rate` is only the static fallback used when the
    # provider is unreachable and no cached rate exists.
    usd_idr_rate: int = 16300
    fx_rate_url: str = "https://open.er-api.com/v6/latest/USD"
    fx_margin_percent: float = 1.0
    fx_ttl_seconds: int = 2 * 60 * 60
    # The background poller refreshes on this interval regardless of traffic, so
    # pricing is current even with no payments that day. Note the default
    # provider only publishes once per ~24h — see app/services/fx.py.
    fx_poll_seconds: int = 2 * 60 * 60
    # Where Snap sends the customer once they finish/cancel.
    # Note the doubled segment: the dashboard runs with Next.js basePath
    # "/dashboard" *and* has an app/dashboard route, so the single-segment
    # form 404s and would land paying customers on a dead page.
    payment_finish_redirect_url: str = (
        "https://dashboard.aivory.id/dashboard/dashboard/payments"
    )
    # How long a Snap payment link stays valid, in hours.
    payment_expiry_hours: int = 24

    # Trusted server-to-server token for calling avry-backend's internal APIs
    # (used to apply entitlements once a payment is verified).
    internal_service_token: Optional[str] = None
    avry_backend_url: str = "http://avry-backend:8081"

    # Notifications. All optional: a missing channel is skipped, never fatal —
    # a receipt that can't be sent must not undo a payment that succeeded.
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from_email: Optional[str] = None
    # Preferred sender for billing mail. The platform's shared SMTP identity is
    # careers@, which is the wrong From: for a receipt.
    payments_from_email: Optional[str] = None

    # Receipts are delivered by the n8n workflow "Aivory - Payment Receipt
    # Email" rather than by this service's own SMTP client, so the template can
    # be edited without redeploying a service that moves money, and every send
    # is visible in n8n's execution list. The token is the only thing guarding
    # that public webhook. With either unset, delivery falls back to SMTP.
    n8n_receipt_webhook_url: Optional[str] = None
    receipt_email_token: Optional[str] = None
    # Admin alerting over the existing Telegram bot.
    telegram_bot_token: Optional[str] = None
    admin_telegram_chat_id: Optional[str] = None


    # n8n Integration configuration
    n8n_base_url: str = "http://43.156.108.96:5678"
    n8n_timeout: float = 10.0
    n8n_max_retries: int = 3
    
    # CORS configuration. A payment API is called with credentials, so "*" is
    # both unsafe and rejected by browsers alongside allow_credentials.
    cors_origins: list[str] = [
        "https://aivory.id",
        "https://www.aivory.id",
        "https://dashboard.aivory.id",
        "https://admin.aivory.id",
        "https://console.aivory.id",
        "https://api.aivory.id",
        "https://aivory.uk",
        "https://www.aivory.uk",
    ]

    def validate_midtrans_config(self) -> None:
        """
        Fail fast on a Midtrans configuration that cannot transact.

        The dangerous case is a *silent* mismatch: production keys pointed at
        the sandbox host (or vice versa) authenticate against nothing, so every
        real payment 401s. Midtrans prefixes sandbox keys with "SB-", which
        makes the intended environment inferable from the key itself.
        """
        if not self.midtrans_server_key or not self.midtrans_client_key:
            if self.midtrans_is_production:
                raise ValueError(
                    "MIDTRANS_IS_PRODUCTION=true but MIDTRANS_SERVER_KEY/"
                    "MIDTRANS_CLIENT_KEY are missing. Refusing to start in "
                    "production with payments in mock mode."
                )
            return

        server_is_sandbox = self.midtrans_server_key.startswith("SB-")
        client_is_sandbox = self.midtrans_client_key.startswith("SB-")

        if server_is_sandbox != client_is_sandbox:
            raise ValueError(
                "Midtrans key pair mismatch: one key is a sandbox (SB-) key and "
                "the other is a production key. Use a matching pair."
            )

        if self.midtrans_is_production and server_is_sandbox:
            raise ValueError(
                "MIDTRANS_IS_PRODUCTION=true but the configured keys are "
                "sandbox (SB-) keys. Set production keys or turn the flag off."
            )

        if not self.midtrans_is_production and not server_is_sandbox:
            raise ValueError(
                "Production Midtrans keys are configured but "
                "MIDTRANS_IS_PRODUCTION is false, so the service would call "
                "api.sandbox.midtrans.com with production credentials and every "
                "payment would fail. Set MIDTRANS_IS_PRODUCTION=true."
            )

    def validate_paid_tier_config(self) -> None:
        """
        Validate that required configuration for paid tiers is present.
        This should be called before processing any paid diagnostic requests.
        """
        if not self.openrouter_api_key or not self.openrouter_api_key.strip():
            raise ValueError(
                "OPENROUTER_API_KEY is required for paid diagnostic tiers. "
                "Please set it in .env.local file."
            )


# Global settings instance
try:
    settings = Settings()
    print(f"✓ Configuration loaded successfully")
    print(f"  - App: {settings.app_name} v{settings.app_version}")
    print(f"  - OpenRouter API: {'Configured' if settings.openrouter_api_key else 'Not configured'}")
except ValidationError as e:
    print(f"✗ Configuration validation failed:")
    for error in e.errors():
        field = '.'.join(str(loc) for loc in error['loc'])
        print(f"  - {field}: {error['msg']}")
    print("\nPlease check your .env.local file and ensure all required variables are set correctly.")
    sys.exit(1)
except Exception as e:
    print(f"✗ Failed to load configuration: {str(e)}")
    sys.exit(1)
