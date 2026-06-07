"""
Centralized Supabase JWT authentication for this independent service.

Auth is centralized on Supabase across the whole platform (homepage, user
dashboard and admin dashboard all authenticate via Supabase). Every service
verifies the Supabase-issued access token using the shared project JWT secret
(SUPABASE_JWT_SECRET) — this works seamlessly across separate domains/repos
because the token is a self-contained JWT and no cross-domain cookies are
needed; the frontend simply sends `Authorization: Bearer <token>`.

For backward compatibility during the migration, a legacy backend-issued HS256
token (signed with JWT_SECRET) is also accepted. A trusted server-to-server
caller (e.g. an admin dashboard BFF route) may authenticate with a shared
INTERNAL_SERVICE_TOKEN.

Endpoints that expose cross-user (admin) data must depend on `require_admin`.
User-scoped endpoints may use `require_auth`; public endpoints stay open.
"""

import os
from typing import Optional

import jwt
from fastapi import Header, HTTPException, Depends

# Supabase project JWT secret — the canonical, platform-wide signing key.
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")
# Legacy backend signing key (transition only).
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
# Optional shared secret for trusted server-to-server (BFF) calls.
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN")

ADMIN_ACCOUNT_TYPES = {"admin", "superadmin"}


def _extract_token(authorization: Optional[str]) -> Optional[str]:
    """Pull the raw token out of an Authorization header value."""
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    # Allow a bare token without the "Bearer" prefix.
    return authorization.strip() or None


def verify_token(token: str) -> Optional[dict]:
    """
    Verify a JWT against the configured secrets (Supabase first, then legacy).

    Returns the decoded payload on success, or None if the token is invalid or
    expired under every configured secret.
    """
    # Supabase is the canonical issuer; check it first.
    secrets = [s for s in (SUPABASE_JWT_SECRET, JWT_SECRET) if s]

    for secret in secrets:
        try:
            # Signature + expiry are enforced; audience is not verified because
            # Supabase uses aud="authenticated" while legacy tokens omit it.
            return jwt.decode(
                token,
                secret,
                algorithms=[JWT_ALGORITHM],
                options={"verify_aud": False},
            )
        except jwt.InvalidTokenError:
            continue
    return None


def _account_type(payload: dict) -> Optional[str]:
    """Resolve the account_type claim across Supabase and legacy token shapes."""
    return (
        payload.get("account_type")
        or (payload.get("user_metadata") or {}).get("account_type")
        or (payload.get("app_metadata") or {}).get("account_type")
    )


async def require_auth(authorization: Optional[str] = Header(None)) -> dict:
    """
    FastAPI dependency: require a valid authenticated caller.

    Accepts a valid Supabase (or legacy) end-user JWT, or the internal service
    token.
    """
    token = _extract_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Missing authentication token")

    if INTERNAL_SERVICE_TOKEN and token == INTERNAL_SERVICE_TOKEN:
        return {"sub": "internal-service", "account_type": "superadmin"}

    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return payload


async def require_admin(payload: dict = Depends(require_auth)) -> dict:
    """
    FastAPI dependency: require an admin/superadmin caller.

    The internal service token is treated as superadmin.
    """
    if _account_type(payload) not in ADMIN_ACCOUNT_TYPES:
        raise HTTPException(status_code=403, detail="Admin access required")
    return payload
