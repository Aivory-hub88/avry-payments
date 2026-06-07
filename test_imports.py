#!/usr/bin/env python
"""
Quick test to verify all modules can be imported correctly.
This helps catch import errors before Docker build.
"""

import sys
import traceback

def test_imports():
    """Test that all key modules can be imported."""
    tests = [
        ("main", lambda: __import__('main')),
        ("app.routes.payment", lambda: __import__('app.routes.payment', fromlist=['router'])),
        ("app.routes.wallet", lambda: __import__('app.routes.wallet', fromlist=['router'])),
        ("app.services.payment_gateway", lambda: __import__('app.services.payment_gateway')),
        ("app.services.wallet_service", lambda: __import__('app.services.wallet_service')),
        ("app.services.payment_validation", lambda: __import__('app.services.payment_validation')),
        ("app.services.purchase_service", lambda: __import__('app.services.purchase_service')),
        ("app.models.wallet", lambda: __import__('app.models.wallet')),
        ("app.models.validation", lambda: __import__('app.models.validation')),
        ("app.models.diagnostic", lambda: __import__('app.models.diagnostic')),
        ("app.models.snapshot", lambda: __import__('app.models.snapshot')),
        ("app.database.db_service", lambda: __import__('app.database.db_service')),
        ("app.config", lambda: __import__('app.config')),
        ("midtrans_config", lambda: __import__('midtrans_config')),
    ]
    
    passed = 0
    failed = 0
    
    print("=" * 60)
    print("IMPORT TESTS")
    print("=" * 60)
    
    for test_name, test_func in tests:
        try:
            test_func()
            print(f"✓ {test_name:45s} OK")
            passed += 1
        except Exception as e:
            print(f"✗ {test_name:45s} FAILED")
            print(f"  Error: {str(e)}")
            traceback.print_exc()
            failed += 1
    
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    
    return failed == 0

if __name__ == "__main__":
    success = test_imports()
    sys.exit(0 if success else 1)
