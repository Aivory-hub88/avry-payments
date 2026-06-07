-- Phase 2: Payments Service Independent Database Schema
-- This creates the independent payments database on postgres-payments:5436
-- Schema: aivery_payments
-- User: payments_user

-- ============================================================================
-- PAYMENTS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    amount DECIMAL(10, 2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'USD',
    status VARCHAR(20),  -- 'pending', 'processing', 'completed', 'failed', 'refunded'
    payment_method VARCHAR(50),  -- 'card', 'paypal', 'bank_transfer'
    external_transaction_id VARCHAR(500) UNIQUE,
    description TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    CONSTRAINT check_amount CHECK (amount > 0)
);

-- ============================================================================
-- SUBSCRIPTIONS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    plan_id UUID NOT NULL,
    status VARCHAR(20),  -- 'active', 'cancelled', 'expired', 'suspended'
    started_at TIMESTAMP,
    renews_at TIMESTAMP,
    cancelled_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- TRANSACTIONS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id UUID NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
    type VARCHAR(50),  -- 'charge', 'refund', 'chargeback'
    amount DECIMAL(10, 2) NOT NULL,
    status VARCHAR(20),  -- 'pending', 'completed', 'failed'
    gateway_response JSONB,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- BILLING HISTORY TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS billing_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subscription_id UUID NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    billing_date TIMESTAMP,
    amount DECIMAL(10, 2),
    status VARCHAR(20),
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- PAYMENT METHODS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS payment_methods (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    type VARCHAR(50),  -- 'card', 'paypal', 'bank'
    is_default BOOLEAN DEFAULT false,
    token VARCHAR(500),
    last_four VARCHAR(4),
    expiry_date DATE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- INVOICES TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS invoices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    subscription_id UUID REFERENCES subscriptions(id),
    invoice_number VARCHAR(50) UNIQUE,
    amount DECIMAL(10, 2),
    status VARCHAR(20),  -- 'draft', 'sent', 'paid', 'overdue'
    issued_at TIMESTAMP,
    due_at TIMESTAMP,
    paid_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- REFUNDS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS refunds (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id UUID NOT NULL REFERENCES payments(id),
    user_id UUID NOT NULL,
    amount DECIMAL(10, 2),
    reason TEXT,
    status VARCHAR(20),  -- 'pending', 'completed', 'failed'
    gateway_refund_id VARCHAR(500),
    created_at TIMESTAMP DEFAULT NOW(),
    processed_at TIMESTAMP
);

-- ============================================================================
-- PRICING PLANS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS pricing_plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100),  -- 'Free', 'Basic', 'Pro', 'Enterprise'
    description TEXT,
    price DECIMAL(10, 2),
    currency VARCHAR(3) DEFAULT 'USD',
    billing_cycle VARCHAR(20),  -- 'monthly', 'yearly', 'one_time'
    features JSONB,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- INDEXES FOR PERFORMANCE
-- ============================================================================
CREATE INDEX idx_payments_user_id ON payments(user_id);
CREATE INDEX idx_payments_status ON payments(status);
CREATE INDEX idx_payments_created_at ON payments(created_at);
CREATE INDEX idx_payments_external_id ON payments(external_transaction_id);

CREATE INDEX idx_subscriptions_user_id ON subscriptions(user_id);
CREATE INDEX idx_subscriptions_status ON subscriptions(status);
CREATE INDEX idx_subscriptions_plan_id ON subscriptions(plan_id);

CREATE INDEX idx_transactions_payment_id ON transactions(payment_id);
CREATE INDEX idx_transactions_status ON transactions(status);

CREATE INDEX idx_billing_history_subscription_id ON billing_history(subscription_id);
CREATE INDEX idx_billing_history_date ON billing_history(billing_date);

CREATE INDEX idx_payment_methods_user_id ON payment_methods(user_id);

CREATE INDEX idx_invoices_user_id ON invoices(user_id);
CREATE INDEX idx_invoices_status ON invoices(status);

CREATE INDEX idx_refunds_payment_id ON refunds(payment_id);
CREATE INDEX idx_refunds_user_id ON refunds(user_id);

-- ============================================================================
-- DEFAULT DATA
-- ============================================================================
INSERT INTO pricing_plans (name, description, price, currency, billing_cycle, features, is_active) VALUES
    ('Free', 'Free tier with basic diagnostics', 0.00, 'USD', 'one_time', '{"diagnostics_per_month": 1, "basic_reports": true}', true),
    ('Basic', 'Basic tier with monthly access', 29.99, 'USD', 'monthly', '{"diagnostics_per_month": 10, "detailed_reports": true}', true),
    ('Pro', 'Professional tier with advanced features', 99.99, 'USD', 'monthly', '{"diagnostics_per_month": 100, "advanced_reports": true, "priority_support": true}', true)
ON CONFLICT DO NOTHING;

-- ============================================================================
-- GRANTS (for payments_user)
-- ============================================================================
GRANT USAGE ON SCHEMA public TO payments_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO payments_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO payments_user;

-- ============================================================================
-- MIGRATION INFO
-- ============================================================================
-- Created: 2026-06-05
-- Version: 1.0
-- Purpose: Phase 2 Payments Service Independent Database Schema
-- Status: Production Ready
