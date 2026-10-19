"""
Admin promo code management: generate, list, activate/deactivate.

Customer-facing validation lives next to checkout in `app.routes.payment`
(`/promo/validate`). Everything here is admin-only and sits under `/promo/admin`
so it cannot be confused with the customer routes.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from app.auth import caller_user_id, require_admin
from app.database import promo_repo
from app.services import pricing, promo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/payments/promo/admin", tags=["promo"])


class GenerateRequest(BaseModel):
    discount_type: str = Field(..., description="percent | fixed_usd")
    discount_value: float = Field(..., gt=0)
    count: int = Field(1, ge=1, le=promo.MAX_BULK, description="How many codes to create")
    # A single custom code: used when count == 1 and the admin wants a memorable one.
    code: Optional[str] = Field(None, max_length=32)
    prefix: Optional[str] = Field(None, max_length=16, description="e.g. LAUNCH → LAUNCH-K7M2Q9XA")
    max_redemptions: Optional[int] = Field(None, ge=1, description="Per code; blank = unlimited")
    max_customers: Optional[int] = Field(
        None, ge=1, description="Different customers who may use each code; blank = unlimited"
    )
    per_user_limit: int = Field(1, ge=1, le=100)
    valid_from: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    products: Optional[List[str]] = Field(
        None, description="Canonical product ids, or a prefix ending in '_' (e.g. credits_)"
    )
    campaign: Optional[str] = Field(None, max_length=64)
    note: Optional[str] = Field(None, max_length=300)
    # Who the code is for. With no custom code or prefix, the generated codes
    # are prefixed from this so each one reads as belonging to its affiliate.
    affiliate_name: Optional[str] = Field(None, max_length=120, description="Person")
    affiliate_company: Optional[str] = Field(None, max_length=120, description="Company")

    @field_validator("discount_type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v not in ("percent", "fixed_usd"):
            raise ValueError("discount_type must be 'percent' or 'fixed_usd'")
        return v


class ActiveRequest(BaseModel):
    active: bool


def _validate(req: GenerateRequest) -> None:
    if req.discount_type == "percent" and req.discount_value > promo.MAX_PERCENT:
        raise HTTPException(
            status_code=400,
            detail=f"A percentage discount can be at most {promo.MAX_PERCENT}%",
        )
    if req.valid_from and req.expires_at and req.expires_at <= req.valid_from:
        raise HTTPException(status_code=400, detail="expires_at must be after valid_from")
    for p in req.products or []:
        if not (pricing.is_sellable(p) or (p.endswith("_") and p != "wallet_topup_")):
            raise HTTPException(status_code=400, detail=f"Unknown product: {p}")
        if p.startswith("wallet_topup"):
            raise HTTPException(
                status_code=400, detail="Wallet top-ups can't be discounted"
            )


@router.post("/generate")
async def generate(req: GenerateRequest, admin: dict = Depends(require_admin)):
    """Create one custom code, or a batch of random ones sharing the same rules."""
    _validate(req)

    if req.code and req.count != 1:
        raise HTTPException(status_code=400, detail="A custom code can only be created one at a time")

    affiliate_name = (req.affiliate_name or "").strip() or None
    affiliate_company = (req.affiliate_company or "").strip() or None

    prefix = ""
    if not req.prefix and not req.code:
        prefix = promo.affiliate_prefix(affiliate_name, affiliate_company)
    if req.prefix:
        prefix = req.prefix.strip().upper()
        try:
            promo.normalize_code(prefix + "-AAA")
        except promo.PromoError:
            raise HTTPException(
                status_code=400, detail="Prefix may only use A-Z, 0-9, '-' and '_'"
            )

    if req.code:
        try:
            codes = [promo.normalize_code(req.code)]
        except promo.PromoError:
            raise HTTPException(
                status_code=400,
                detail="Code must be 3-32 characters: A-Z, 0-9, '-' or '_'",
            )
    else:
        # Over-draw slightly so a rare collision doesn't leave the batch short.
        seen: set[str] = set()
        while len(seen) < req.count:
            seen.add(promo.generate_code(prefix))
        codes = sorted(seen)

    base = {
        "discount_type": req.discount_type,
        "discount_value": round(req.discount_value, 2),
        "max_redemptions": req.max_redemptions,
        "max_customers": req.max_customers,
        "per_user_limit": req.per_user_limit,
        "valid_from": req.valid_from,
        "expires_at": req.expires_at,
        "products": req.products or None,
        "campaign": (req.campaign or "").strip() or None,
        "note": (req.note or "").strip() or None,
        "created_by": caller_user_id(admin),
        "affiliate_name": affiliate_name,
        "affiliate_company": affiliate_company,
    }
    created = promo_repo.insert_many([{**base, "code": c} for c in codes])

    if req.code and not created:
        raise HTTPException(status_code=409, detail="That code already exists")

    logger.info(
        "Promo codes generated: %d (%s %s) by %s",
        len(created), req.discount_type, req.discount_value, caller_user_id(admin),
    )
    return {"success": True, "created": len(created), "codes": created}


@router.get("")
async def list_codes(
    _admin: dict = Depends(require_admin),
    search: Optional[str] = None,
    campaign: Optional[str] = None,
    status: Optional[str] = Query(None, pattern="^(active|inactive|expired|exhausted)$"),
    affiliate: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    rows, total = promo_repo.list_codes(
        search=search, campaign=campaign, status=status, affiliate=affiliate,
        limit=page_size, offset=(page - 1) * page_size,
    )
    return {
        "success": True,
        "codes": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, -(-total // page_size)),
        "campaigns": promo_repo.campaigns(),
    }


@router.get("/export")
async def export_codes(
    _admin: dict = Depends(require_admin),
    campaign: Optional[str] = None,
    status: Optional[str] = Query(None, pattern="^(active|inactive|expired|exhausted)$"),
    affiliate: Optional[str] = None,
):
    """CSV of the codes matching the filter — the handoff for a batch to marketing,
    and the per-code conversion figures for affiliate reporting."""
    rows, _ = promo_repo.list_codes(
        campaign=campaign, status=status, affiliate=affiliate, limit=500
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "code", "affiliate_company", "affiliate_name", "discount_type", "discount_value",
        "max_customers", "customers_used", "max_redemptions", "redeemed", "per_user_limit", "valid_from", "expires_at",
        "active", "campaign", "note", "applied_customers", "checkout_customers",
        "paid_customers", "conversion_rate", "revenue_usd", "discount_given_usd",
    ])
    for r in rows:
        w.writerow([
            r["code"], r["affiliate_company"] or "", r["affiliate_name"] or "",
            r["discount_type"], r["discount_value"], r["max_customers"] or "",
            r["customers_used"], r["max_redemptions"] or "",
            r["redeemed"], r["per_user_limit"], r["valid_from"] or "", r["expires_at"] or "",
            "yes" if r["active"] else "no", r["campaign"] or "", r["note"] or "",
            r["applied_users"], r["checkout_users"], r["paid_users"],
            r["conversion_rate"], r["revenue_usd"], r["discount_given_usd"],
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="aivory-promo-codes.csv"'},
    )


@router.get("/affiliates")
async def list_affiliates(_admin: dict = Depends(require_admin)):
    """Lead conversion per affiliate: customers who applied a code, started
    checkout, paid, and the revenue those codes brought in."""
    return {"success": True, "affiliates": promo_repo.affiliates()}


@router.get("/{code}/leads")
async def code_leads(code: str, _admin: dict = Depends(require_admin)):
    """The customers behind one code, each at the furthest stage they reached."""
    try:
        norm = promo.normalize_code(code)
    except promo.PromoError:
        raise HTTPException(status_code=404, detail="Unknown promo code")
    if not promo_repo.get(norm):
        raise HTTPException(status_code=404, detail="Unknown promo code")
    return {"success": True, "code": norm, "leads": promo_repo.leads(norm)}


@router.patch("/{code}")
async def set_active(code: str, req: ActiveRequest, _admin: dict = Depends(require_admin)):
    try:
        norm = promo.normalize_code(code)
    except promo.PromoError:
        raise HTTPException(status_code=404, detail="Unknown promo code")
    if not promo_repo.set_active(norm, req.active):
        raise HTTPException(status_code=404, detail="Unknown promo code")
    return {"success": True, "code": norm, "active": req.active}
