"""
Midtrans Payment Gateway Configuration

This module provides configuration and utility functions for Midtrans integration.

Usage:
    from midtrans_config import midtrans_config
    
    # Check if Midtrans is configured
    if midtrans_config.is_configured:
        # Get client key for frontend
        client_key = midtrans_config.client_key
"""

import os
from typing import Optional
from dataclasses import dataclass


@dataclass
class MidtransConfig:
    """Midtrans configuration settings."""
    
    server_key: Optional[str] = None
    client_key: Optional[str] = None
    is_production: bool = False
    snap_url: str = ""
    api_url: str = ""
    
    # Product prices (USD)
    snapshot_price_usd: int = 29
    blueprint_price_usd: int = 85
    foundation_price_usd: int = 20
    pro_price_usd: int = 44
    enterprise_price_usd: int = 499
    
    # Credit prices (USD)
    credit_prices_usd: dict = None
    
    def __post_init__(self):
        """Initialize configuration from environment variables."""
        self.server_key = os.getenv("MIDTRANS_SERVER_KEY")
        self.client_key = os.getenv("MIDTRANS_CLIENT_KEY")
        self.is_production = os.getenv("MIDTRANS_IS_PRODUCTION", "false").lower() == "true"
        
        # Set credit prices
        if self.credit_prices_usd is None:
            self.credit_prices_usd = {
                50: 5,
                100: 9,
                250: 20,
                500: 38,
                1000: 70,
                2500: 165,
                5000: 300,
                10000: 550,
            }
        
        # Set URLs based on environment
        if self.is_production:
            self.snap_url = "https://app.midtrans.com/snap/v4"
            self.api_url = "https://api.midtrans.com"
        else:
            self.snap_url = "https://app.sandbox.midtrans.com/snap/v4"
            self.api_url = "https://api.sandbox.midtrans.com"
    
    @property
    def is_configured(self) -> bool:
        """Check if Midtrans is properly configured."""
        return bool(self.server_key and self.client_key)
    
    def get_client_key(self) -> Optional[str]:
        """Get Midtrans client key for frontend."""
        return self.client_key
    
    def convert_usd_to_idr(self, usd_amount: float) -> int:
        """
        Convert USD amount to IDR (Indonesian Rupiah).
        
        Midtrans requires amounts in IDR (smallest currency unit).
        Uses a simple exchange rate (1 USD = 15,000 IDR).
        
        Args:
            usd_amount: Amount in USD
            
        Returns:
            Amount in IDR (as integer)
        """
        # Simple exchange rate: 1 USD = 15,000 IDR
        # In production, you'd want to use a real-time exchange rate API
        exchange_rate = 15000
        return int(usd_amount * exchange_rate)
    
    def get_product_prices_idr(self) -> dict:
        """
        Get product prices in IDR.
        
        Returns:
            Dictionary with product names and IDR prices
        """
        prices = {
            "ai_snapshot": self.convert_usd_to_idr(self.snapshot_price_usd),
            "ai_blueprint": self.convert_usd_to_idr(self.blueprint_price_usd),
            "foundation": self.convert_usd_to_idr(self.foundation_price_usd),
            "pro": self.convert_usd_to_idr(self.pro_price_usd),
            "enterprise": self.convert_usd_to_idr(self.enterprise_price_usd),
        }
        
        # Add credit products
        for credits, usd_price in self.credit_prices_usd.items():
            prices[f"credits_{credits}"] = self.convert_usd_to_idr(usd_price)
        
        return prices


# Global configuration instance
midtrans_config = MidtransConfig()
