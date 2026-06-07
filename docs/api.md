# AVRY Payments Service - API Documentation

## Overview

The AVRY Payments Service provides REST API endpoints for payment processing, wallet management, and subscription handling.

**Base URL (Local)**: `http://localhost:3030`  
**Base URL (Production)**: `https://payments.aivery.io` (via Traefik gateway)

## Authentication

All protected endpoints require a valid JWT token in the Authorization header:

```
Authorization: Bearer <JWT_TOKEN>
```

JWT tokens are issued by AVRY-backend service. Include the token with all authenticated requests.

## Response Format

All responses follow a consistent JSON format:

**Success Response:**
```json
{
  "success": true,
  "data": { /* response data */ }
}
```

**Error Response:**
```json
{
  "success": false,
  "error": "Error message describing what went wrong"
}
```

## Endpoints

### Health Check

#### `GET /health`

Service health check endpoint. No authentication required.

**Response:**
```json
{
  "status": "healthy",
  "service": "avry-payments",
  "version": "1.0.0"
}
```

### Service Info

#### `GET /`

Service information endpoint.

**Response:**
```json
{
  "service": "AVRY Payments Service",
  "version": "1.0.0"
}
```

---

## Payment Endpoints

### Create Payment Transaction

#### `POST /api/v1/payments/midtrans/create`

Create a new payment transaction with Midtrans.

**Authentication**: Required (JWT)

**Request Body:**
```json
{
  "user_id": "user123",
  "amount": 9.99,
  "product": "ai_blueprint",
  "customer_email": "user@example.com",
  "customer_first_name": "John",
  "custom_field1": "optional_field_1",
  "custom_field2": "optional_field_2"
}
```

**Parameters:**
- `user_id` (string, required) - User identifier
- `amount` (float, required, min: 1) - Payment amount in USD
- `product` (string, required) - Product code (e.g., ai_blueprint, credits_100)
- `customer_email` (string, optional) - Customer email
- `customer_first_name` (string, optional) - Customer first name
- `custom_field1` (string, optional) - Custom metadata
- `custom_field2` (string, optional) - Custom metadata

**Response Success:**
```json
{
  "success": true,
  "order_id": "ORDER123",
  "token": "SNAP_TOKEN_123456",
  "redirect_url": "https://app.sandbox.midtrans.com/snap/v4/redirection/...",
  "transaction_id": "TRANS123"
}
```

**Response Error:**
```json
{
  "success": false,
  "error": "Invalid product format"
}
```

---

### Get Transaction Status

#### `GET /api/v1/payments/midtrans/status/{order_id}`

Check the status of a payment transaction.

**Authentication**: Required (JWT)

**URL Parameters:**
- `order_id` (string, required) - Order ID from transaction creation

**Response:**
```json
{
  "success": true,
  "order_id": "ORDER123",
  "transaction_id": "TRANS123",
  "transaction_status": "settlement",
  "payment_type": "credit_card",
  "fraud_status": "accept",
  "gross_amount": "99900"
}
```

---

### Midtrans Webhook

#### `POST /api/v1/payments/midtrans/webhook`

Handle Midtrans webhook notifications. Called by Midtrans when payment status changes.

**Authentication**: Not required (webhook from external service)

**Request Body:**
```json
{
  "transaction_id": "TRANS123",
  "order_id": "ORDER123",
  "transaction_status": "settlement",
  "payment_type": "credit_card",
  "gross_amount": "99900",
  "fraud_status": "accept"
}
```

**Response:**
```json
{
  "success": true,
  "message": "Webhook processed successfully"
}
```

---

### Process Refund

#### `POST /api/v1/payments/midtrans/refund`

Process a refund for a completed payment.

**Authentication**: Required (JWT, admin only)

**Request Body:**
```json
{
  "order_id": "ORDER123",
  "amount": 9.99
}
```

**Parameters:**
- `order_id` (string, required) - Order ID to refund
- `amount` (float, optional) - Amount to refund (full amount if not specified)

**Response:**
```json
{
  "success": true,
  "order_id": "ORDER123",
  "refund_id": "REFUND123",
  "refund_amount": "99900"
}
```

---

### Confirm Payment

#### `POST /api/v1/payments/confirm`

Confirm a payment and process the purchase (update user tier, credits, etc).

**Authentication**: Required (JWT)

**Request Body:**
```json
{
  "order_id": "ORDER123",
  "user_id": "user123",
  "product": "ai_blueprint",
  "amount": 9.99,
  "is_mock": false
}
```

**Parameters:**
- `order_id` (string, required) - Midtrans order ID
- `user_id` (string, required) - User identifier
- `product` (string, required) - Product purchased
- `amount` (float, required) - Amount paid
- `is_mock` (boolean) - Whether this is a mock/test payment

**Response:**
```json
{
  "success": true,
  "message": "Payment confirmed and purchase processed",
  "is_mock": false,
  "data": {
    "user_tier": "pro",
    "credits_added": 100
  }
}
```

---

### Get Payment Configuration

#### `GET /api/v1/payments/config`

Get payment configuration for frontend (prices, client key, etc).

**Authentication**: Not required

**Response:**
```json
{
  "success": true,
  "is_configured": true,
  "is_production": false,
  "client_key": "SB-Mid-client-key",
  "products": {
    "ai_snapshot": {
      "name": "AI Snapshot",
      "price_usd": 29,
      "price_idr": 435000
    },
    "ai_blueprint": {
      "name": "AI System Blueprint",
      "price_usd": 85,
      "price_idr": 1275000
    }
  }
}
```

---

### Get Midtrans Client Key

#### `GET /api/v1/payments/client-key`

Get Midtrans client key for frontend Snap integration.

**Authentication**: Not required

**Response:**
```json
{
  "success": true,
  "client_key": "SB-Mid-client-key",
  "is_production": false
}
```

---

### Get Payment History

#### `GET /api/v1/payments/history/{user_id}`

Get payment history for a user.

**Authentication**: Required (JWT)

**URL Parameters:**
- `user_id` (string, required) - User identifier

**Response:**
```json
{
  "success": true,
  "payments": [
    {
      "payment_id": "PAY123",
      "order_id": "ORDER123",
      "user_id": "user123",
      "product": "ai_blueprint",
      "amount": 9.99,
      "status": "completed",
      "payment_method": "midtrans",
      "is_mock": false,
      "transaction_id": "TRANS123",
      "created_at": "2024-01-15T10:30:00Z",
      "updated_at": "2024-01-15T10:35:00Z"
    }
  ],
  "total": 1
}
```

---

### Record Manual Payment

#### `POST /api/v1/payments/record`

Record a manual payment (admin endpoint).

**Authentication**: Required (JWT, admin only)

**Query Parameters:**
- `user_id` (string, required) - User identifier
- `amount` (float, required) - Payment amount
- `payment_method` (string, optional, default: "manual") - Payment method
- `product` (string, optional, default: "ai_blueprint") - Product purchased

**Response:**
```json
{
  "success": true,
  "message": "Payment recorded successfully",
  "user_id": "user123",
  "amount": 9.99,
  "product": "ai_blueprint"
}
```

---

## Wallet Endpoints

### Get Wallet Information

#### `GET /api/v1/wallet/{user_id}`

Get user wallet information including balance and history.

**Authentication**: Required (JWT)

**URL Parameters:**
- `user_id` (string, required) - User identifier

**Response:**
```json
{
  "success": true,
  "wallet": {
    "user_id": "user123",
    "credits": 250.50,
    "wallet_balance": 100.00,
    "total_spent": 450.00,
    "total_credits_purchased": 500,
    "is_active": true,
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-01-15T10:30:00Z"
  }
}
```

---

### Deduct from Wallet

#### `POST /api/v1/wallet/deduct`

Deduct credits/funds from user wallet.

**Authentication**: Required (JWT, service-to-service only)

**Request Body:**
```json
{
  "user_id": "user123",
  "amount": 50.00,
  "reason": "Blueprint generation"
}
```

**Response:**
```json
{
  "success": true,
  "message": "Credits deducted successfully",
  "new_balance": 200.50,
  "amount_deducted": 50.00
}
```

---

### Add to Wallet

#### `POST /api/v1/wallet/add`

Add credits/funds to user wallet (purchase, refund, admin adjustment).

**Authentication**: Required (JWT, admin only)

**Request Body:**
```json
{
  "user_id": "user123",
  "amount": 100.00,
  "reason": "Promotional credit"
}
```

**Response:**
```json
{
  "success": true,
  "message": "Credits added successfully",
  "new_balance": 350.50,
  "amount_added": 100.00
}
```

---

## Error Codes

| Code | Status | Description |
|------|--------|-------------|
| 200 | OK | Request successful |
| 400 | Bad Request | Invalid request parameters |
| 401 | Unauthorized | Missing or invalid JWT token |
| 403 | Forbidden | User doesn't have permission |
| 404 | Not Found | Resource not found |
| 422 | Unprocessable Entity | Invalid data format |
| 500 | Internal Server Error | Server error |

---

## Rate Limiting

API requests are rate-limited to:
- 100 requests per minute for authenticated users
- 20 requests per minute for unauthenticated endpoints

Exceeding limits returns `429 Too Many Requests`.

---

## Examples

### Complete Payment Flow

1. **Frontend gets client key:**
   ```bash
   GET /api/v1/payments/client-key
   ```

2. **Frontend initiates payment:**
   ```bash
   POST /api/v1/payments/midtrans/create
   Body: {
     "user_id": "user123",
     "amount": 9.99,
     "product": "ai_blueprint"
   }
   ```

3. **Frontend displays Midtrans Snap popup** using returned token

4. **User completes payment** in Midtrans

5. **Midtrans calls webhook** (backend):
   ```bash
   POST /api/v1/payments/midtrans/webhook
   ```

6. **Frontend confirms payment:**
   ```bash
   POST /api/v1/payments/confirm
   Body: {
     "order_id": "ORDER123",
     "user_id": "user123",
     "product": "ai_blueprint",
     "amount": 9.99
   }
   ```

7. **User's tier/credits updated**, payment complete

---

## Testing

### Using cURL

Get client key:
```bash
curl -X GET http://localhost:3030/api/v1/payments/client-key
```

Create payment (with JWT token):
```bash
curl -X POST http://localhost:3030/api/v1/payments/midtrans/create \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "amount": 9.99,
    "product": "ai_blueprint",
    "customer_email": "test@example.com"
  }'
```

Get payment history:
```bash
curl -X GET http://localhost:3030/api/v1/payments/history/user123 \
  -H "Authorization: Bearer YOUR_JWT_TOKEN"
```

---

## SDK Usage

### JavaScript/TypeScript

```typescript
// Get client key
const response = await fetch('http://localhost:3030/api/v1/payments/client-key');
const { client_key } = await response.json();

// Create payment
const paymentResponse = await fetch('http://localhost:3030/api/v1/payments/midtrans/create', {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${jwtToken}`,
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    user_id: 'user123',
    amount: 9.99,
    product: 'ai_blueprint',
    customer_email: 'user@example.com'
  })
});

const { token, redirect_url } = await paymentResponse.json();

// Use token in Midtrans Snap
snap.pay(token, {
  onSuccess: (result) => confirmPayment(result),
  onPending: (result) => console.log('Pending', result),
  onError: (error) => console.error('Error', error)
});
```

---

## Support

For API issues:
1. Check this documentation
2. Review service logs: `docker-compose logs avry-payments`
3. Check Midtrans dashboard for payment status
4. Contact AIVERY support team
