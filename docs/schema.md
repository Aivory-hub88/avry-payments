# AVRY Payments Service Database Schema

## Overview

AVRY Payments service manages all payment transactions, wallets, subscriptions, and financial records. Data is stored in the `aivery_payments` schema within the Supabase PostgreSQL database.

## Current Implementation

During MVP/Phase 1, the service uses JSON file storage for rapid development. Tables below show the schema that will be created when migrating to PostgreSQL.

## Database Tables

### 1. `payments` - Payment Transactions

Stores all payment transactions processed through Midtrans or other payment gateways.

```sql
CREATE TABLE payments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  payment_id VARCHAR(50) UNIQUE NOT NULL,
  order_id VARCHAR(100) UNIQUE NOT NULL,
  user_id UUID NOT NULL REFERENCES users(id),
  product VARCHAR(100) NOT NULL,  -- ai_blueprint, ai_snapshot, credits_100, etc
  amount DECIMAL(10, 2) NOT NULL,
  currency VARCHAR(3) DEFAULT 'USD',
  status VARCHAR(50) DEFAULT 'pending',  -- pending, settlement, capture, deny, expire, cancel
  payment_method VARCHAR(50),  -- credit_card, bank_transfer, etc
  payment_gateway VARCHAR(50),  -- midtrans, stripe, etc
  transaction_id VARCHAR(100),  -- External payment gateway transaction ID
  is_mock BOOLEAN DEFAULT FALSE,  -- Flag for test payments
  
  -- Midtrans specific fields
  payment_type VARCHAR(50),
  fraud_status VARCHAR(50),
  gross_amount VARCHAR(50),
  
  -- Metadata
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMP,
  failed_at TIMESTAMP,
  
  CONSTRAINT valid_amount CHECK (amount > 0),
  CONSTRAINT valid_payment_status CHECK (status IN ('pending', 'settlement', 'capture', 'deny', 'expire', 'cancel'))
);

-- Indexes
CREATE INDEX idx_payments_user_id ON payments(user_id);
CREATE INDEX idx_payments_order_id ON payments(order_id);
CREATE INDEX idx_payments_status ON payments(status);
CREATE INDEX idx_payments_created_at ON payments(created_at);
```

### 2. `wallet` - User Wallet Balances

Tracks user credit balances, wallet funds, and account balance.

```sql
CREATE TABLE wallet (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL UNIQUE REFERENCES users(id),
  credits DECIMAL(12, 2) DEFAULT 0,  -- Credit balance (in service currency)
  wallet_balance DECIMAL(12, 2) DEFAULT 0,  -- Wallet balance
  total_spent DECIMAL(12, 2) DEFAULT 0,  -- Total amount user has spent
  total_credits_purchased INT DEFAULT 0,  -- Total credits purchased
  
  -- Account status
  is_active BOOLEAN DEFAULT TRUE,
  last_activity TIMESTAMP,
  
  -- Timestamps
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  
  CONSTRAINT non_negative_credits CHECK (credits >= 0),
  CONSTRAINT non_negative_balance CHECK (wallet_balance >= 0)
);

-- Indexes
CREATE INDEX idx_wallet_user_id ON wallet(user_id);
CREATE INDEX idx_wallet_is_active ON wallet(is_active);
```

### 3. `wallet_transactions` - Transaction History

Detailed log of all wallet debits and credits.

```sql
CREATE TABLE wallet_transactions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id),
  transaction_type VARCHAR(50) NOT NULL,  -- credit, debit, refund, adjustment
  amount DECIMAL(12, 2) NOT NULL,
  description TEXT,
  reference_type VARCHAR(50),  -- payment_id, refund_id, admin_adjustment, etc
  reference_id VARCHAR(100),
  
  -- Pre and post balances for audit trail
  balance_before DECIMAL(12, 2),
  balance_after DECIMAL(12, 2),
  
  -- Metadata
  created_by VARCHAR(100),  -- User ID or 'system' or 'admin'
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  
  CONSTRAINT valid_transaction_type CHECK (
    transaction_type IN ('credit', 'debit', 'refund', 'adjustment', 'transfer')
  )
);

-- Indexes
CREATE INDEX idx_wallet_transactions_user_id ON wallet_transactions(user_id);
CREATE INDEX idx_wallet_transactions_created_at ON wallet_transactions(created_at);
CREATE INDEX idx_wallet_transactions_type ON wallet_transactions(transaction_type);
```

### 4. `subscriptions` - User Subscriptions

Tracks active subscriptions and billing cycles.

```sql
CREATE TABLE subscriptions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  subscription_id VARCHAR(100) UNIQUE NOT NULL,
  user_id UUID NOT NULL REFERENCES users(id),
  tier_id VARCHAR(100) NOT NULL,  -- foundation, pro, enterprise, etc
  
  -- Billing cycle
  status VARCHAR(50) DEFAULT 'active',  -- active, past_due, cancelled, expired
  billing_cycle_start DATE,
  billing_cycle_end DATE,
  next_billing_date DATE,
  
  -- Payment info
  payment_method VARCHAR(50),
  auto_renew BOOLEAN DEFAULT TRUE,
  
  -- Pricing
  monthly_price DECIMAL(10, 2),
  annual_price DECIMAL(10, 2),
  discount_percentage INT DEFAULT 0,
  
  -- Trial period
  trial_end_date DATE,
  is_trial BOOLEAN DEFAULT FALSE,
  
  -- Cancellation
  cancelled_at TIMESTAMP,
  cancellation_reason TEXT,
  
  -- Timestamps
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  
  CONSTRAINT valid_status CHECK (status IN ('active', 'past_due', 'cancelled', 'expired', 'pending'))
);

-- Indexes
CREATE INDEX idx_subscriptions_user_id ON subscriptions(user_id);
CREATE INDEX idx_subscriptions_status ON subscriptions(status);
CREATE INDEX idx_subscriptions_tier_id ON subscriptions(tier_id);
```

### 5. `refunds` - Refund Records

Tracks refund requests and their status.

```sql
CREATE TABLE refunds (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  refund_id VARCHAR(100) UNIQUE NOT NULL,
  payment_id UUID NOT NULL REFERENCES payments(id),
  user_id UUID NOT NULL REFERENCES users(id),
  
  -- Refund details
  original_amount DECIMAL(10, 2) NOT NULL,
  refund_amount DECIMAL(10, 2) NOT NULL,
  reason VARCHAR(255),
  
  -- Status
  status VARCHAR(50) DEFAULT 'pending',  -- pending, completed, failed, rejected
  
  -- External gateway info
  gateway_refund_id VARCHAR(100),
  
  -- Timestamps
  requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  processed_at TIMESTAMP,
  completed_at TIMESTAMP,
  
  CONSTRAINT valid_status CHECK (status IN ('pending', 'completed', 'failed', 'rejected')),
  CONSTRAINT valid_amount CHECK (refund_amount > 0 AND refund_amount <= original_amount)
);

-- Indexes
CREATE INDEX idx_refunds_user_id ON refunds(user_id);
CREATE INDEX idx_refunds_payment_id ON refunds(payment_id);
CREATE INDEX idx_refunds_status ON refunds(status);
```

### 6. `payment_cards` - Saved Payment Cards

Stores saved payment card information (tokenized for security).

```sql
CREATE TABLE payment_cards (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id),
  
  -- Card info (tokenized, never store full card numbers)
  card_token VARCHAR(255) UNIQUE NOT NULL,  -- Midtrans token
  card_brand VARCHAR(50),  -- visa, mastercard, amex, etc
  card_last_four VARCHAR(4),
  card_holder_name VARCHAR(100),
  
  -- Expiry
  expiry_month INT,
  expiry_year INT,
  
  -- Status
  is_default BOOLEAN DEFAULT FALSE,
  is_active BOOLEAN DEFAULT TRUE,
  verified_at TIMESTAMP,
  
  -- Timestamps
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  deleted_at TIMESTAMP
);

-- Indexes
CREATE INDEX idx_payment_cards_user_id ON payment_cards(user_id);
CREATE INDEX idx_payment_cards_is_default ON payment_cards(is_default);
```

### 7. `invoices` - Invoice Records

Stores generated invoices for accounting/bookkeeping.

```sql
CREATE TABLE invoices (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id VARCHAR(100) UNIQUE NOT NULL,
  payment_id UUID REFERENCES payments(id),
  subscription_id UUID REFERENCES subscriptions(id),
  user_id UUID NOT NULL REFERENCES users(id),
  
  -- Invoice details
  invoice_date DATE,
  due_date DATE,
  total_amount DECIMAL(10, 2) NOT NULL,
  tax_amount DECIMAL(10, 2) DEFAULT 0,
  discount_amount DECIMAL(10, 2) DEFAULT 0,
  
  -- Status
  status VARCHAR(50) DEFAULT 'draft',  -- draft, sent, paid, overdue, cancelled
  
  -- Timestamps
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  sent_at TIMESTAMP,
  paid_at TIMESTAMP
);

-- Indexes
CREATE INDEX idx_invoices_user_id ON invoices(user_id);
CREATE INDEX idx_invoices_status ON invoices(status);
```

### 8. `audit_log` - Financial Audit Trail

Tracks all financial transactions for compliance and debugging.

```sql
CREATE TABLE audit_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  action VARCHAR(100) NOT NULL,  -- payment_created, payment_confirmed, refund_issued, etc
  entity_type VARCHAR(50) NOT NULL,  -- payment, wallet, subscription, refund
  entity_id VARCHAR(100) NOT NULL,
  user_id UUID REFERENCES users(id),
  
  -- Changes
  old_values JSONB,
  new_values JSONB,
  
  -- Details
  reason TEXT,
  ip_address VARCHAR(45),
  user_agent TEXT,
  
  -- Timestamp
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE INDEX idx_audit_log_entity ON audit_log(entity_type, entity_id);
CREATE INDEX idx_audit_log_user_id ON audit_log(user_id);
CREATE INDEX idx_audit_log_action ON audit_log(action);
CREATE INDEX idx_audit_log_created_at ON audit_log(created_at);
```

## Schema Migration

When migrating from JSON storage to PostgreSQL:

1. Create `aivery_payments` schema
2. Create all tables listed above
3. Migrate existing JSON data to PostgreSQL
4. Update connection string in `.env`
5. Update database service to use SQLAlchemy ORM

## Constraints & Validations

- **User References**: All user_id fields reference the `users` table in `aivery_backend` schema
- **Amounts**: All monetary amounts stored as DECIMAL(10, 2) with proper validation
- **Timestamps**: All timestamps in UTC
- **Audit Trail**: All sensitive operations logged in audit_log table

## Indexes

Key indexes for performance:

- `payments(user_id, created_at)` - Fast user payment history
- `wallet(user_id)` - Fast wallet balance lookup
- `wallet_transactions(user_id, created_at)` - Transaction history queries
- `subscriptions(user_id, status)` - Active subscription lookup
- `payment_cards(user_id, is_default)` - Default card lookup

## Backup & Recovery

The `aivery_payments` schema should be backed up:

- Daily automated backups via Supabase
- Point-in-time recovery enabled
- Monthly archived backups stored externally

## Migration Timeline

- **Week 1**: Use JSON file storage for MVP testing
- **Week 2-3**: Create PostgreSQL schema in Supabase
- **Week 4**: Migrate data from JSON to PostgreSQL
- **Week 5+**: Full PostgreSQL production deployment

## Access Control

Database access for AVRY-payments service:

- Service connects with dedicated role: `avry_payments_role`
- Role has SELECT, INSERT, UPDATE on `aivery_payments.*`
- Role has no DELETE permissions (soft deletes only)
- Role cannot access other schemas (aivery_backend, aivery_diagnostics, etc)

## Testing

SQL scripts for testing schema:

```sql
-- Verify schema exists
SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'aivery_payments';

-- Verify all tables exist
SELECT table_name FROM information_schema.tables WHERE table_schema = 'aivery_payments';

-- Check table sizes
SELECT schemaname, tablename, pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) 
FROM pg_tables 
WHERE schemaname = 'aivery_payments' 
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
```
