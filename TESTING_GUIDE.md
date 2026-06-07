# AVRY-payments Local Testing Guide

This guide walks through testing the AVRY-payments microservice locally with Docker.

## Prerequisites

- Docker and Docker Compose installed
- `.env.local` configured (should already be in place)
- Port 3030 available (or modify docker-compose.yml)

## Step 1: Build the Docker Image

```bash
cd services/avry-payments
docker-compose build
```

Expected output:
```
Building avry_payments
Step 1/X : FROM python:3.11-slim
...
Successfully tagged avry-payments:latest
```

## Step 2: Start the Service

```bash
docker-compose up
```

You should see output like:
```
avry-payments  | ✓ Configuration loaded successfully
avry-payments  |   - App: Aivory AI Readiness Platform v1.0.0
avry-payments  |   - OpenRouter API: Configured
avry-payments  | INFO:     Uvicorn running on http://0.0.0.0:3030
```

## Step 3: Test Health Endpoint

In another terminal:

```bash
curl http://localhost:3030/health
```

Expected response (200 OK):
```json
{
  "status": "healthy",
  "service": "avry-payments",
  "version": "1.0.0"
}
```

## Step 4: Test Payment Configuration

```bash
curl http://localhost:3030/api/v1/payments/config
```

Expected response:
```json
{
  "success": true,
  "is_configured": false,  // or true if Midtrans keys configured
  "is_production": false,
  "client_key": "SB-Mid-client-test-key-12345",
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

## Step 5: Test Payment Creation

```bash
curl -X POST http://localhost:3030/api/v1/payments/midtrans/create \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "test_user_123",
    "amount": 9.99,
    "product": "ai_blueprint",
    "customer_email": "test@example.com",
    "customer_first_name": "Test"
  }'
```

Expected response (in mock mode):
```json
{
  "success": true,
  "is_mock": true,
  "order_id": "payment_ai_blueprint_a7f3k9m2p5q1",
  "token": "mock_token_payment_ai_blueprint_a7f3k9m2p5q1",
  "redirect_url": "https://app.sandbox.midtrans.com/snap/v1/redirection/payment_ai_blueprint_a7f3k9m2p5q1",
  "transaction_id": "mock_txn_payment_ai_blueprint_a7f3k9m2p5q1"
}
```

## Step 6: Test Wallet Endpoints

### Get wallet info:
```bash
curl http://localhost:3030/api/v1/wallet/test_user_123
```

Expected response:
```json
{
  "success": true,
  "wallet_id": "wallet_x4j8n2v7c9k3",
  "balance": 0.0,
  "total_topup": 0.0,
  "total_spent": 0.0,
  "total_refunded": 0.0,
  "currency": "USD",
  "cards": [],
  "last_transaction_at": null
}
```

### Get wallet balance:
```bash
curl http://localhost:3030/api/v1/wallet/test_user_123/balance
```

### Get transaction history:
```bash
curl http://localhost:3030/api/v1/wallet/test_user_123/transactions
```

## Step 7: Test Payment Confirmation

```bash
curl -X POST http://localhost:3030/api/v1/payments/confirm \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": "payment_ai_blueprint_a7f3k9m2p5q1",
    "user_id": "test_user_123",
    "product": "ai_blueprint",
    "amount": 85.0,
    "is_mock": true
  }'
```

Expected response:
```json
{
  "success": true,
  "message": "Payment confirmed and purchase processed",
  "is_mock": true,
  "data": {
    "user_id": "test_user_123",
    "product": "ai_blueprint",
    "tier": "...",
    "updated_at": "2024-..."
  }
}
```

## Step 8: Parallel Testing with Monolith

### Terminal 1 - Start Payments Service (already running)
```bash
# In services/avry-payments directory
docker-compose up
```

### Terminal 2 - Start Monolith
```bash
# In the monolith directory
cd ..
python main.py  # or docker-compose up if using Docker
# Monolith runs on port 8000
```

### Terminal 3 - Run Comparison Tests

```bash
# Test payments endpoint on both services
# Monolith (old)
curl http://localhost:8000/api/v1/payments/config

# Payments service (new)
curl http://localhost:3030/api/v1/payments/config

# Compare responses - should be identical
```

## Testing Troubleshooting

### Service won't start

Check logs:
```bash
docker-compose logs avry-payments
```

Common issues:
- Port 3030 already in use: `lsof -i :3030` and kill the process
- Database connection error: Check DATABASE_URL in .env.local
- Import errors: Should have been caught, check logs for details

### Health check failing

```bash
# Check if service is responding at all
curl -v http://localhost:3030/health

# View real-time logs
docker-compose logs -f avry-payments
```

### Payment endpoints returning errors

1. Check Midtrans configuration:
   ```bash
   grep MIDTRANS .env.local
   ```

2. In mock mode, keys can be anything. Verify JSON format is correct.

3. Check logs for specific error messages:
   ```bash
   docker-compose logs avry-payments | grep -i error
   ```

## Performance Testing

### Load test the health endpoint:

```bash
# Using Apache Bench (ab) - test 100 requests, 10 concurrent
ab -n 100 -c 10 http://localhost:3030/health
```

Expected performance:
- Response time: < 50ms per request
- No failures (0 failed requests)

### Load test payment creation:

```bash
# Test payment endpoint (this will create mock transactions)
for i in {1..10}; do
  curl -X POST http://localhost:3030/api/v1/payments/midtrans/create \
    -H "Content-Type: application/json" \
    -d "{
      \"user_id\": \"load_test_user_$i\",
      \"amount\": 9.99,
      \"product\": \"ai_blueprint\"
    }"
done
```

## Stopping the Service

```bash
# Stop and remove containers
docker-compose down

# Or just stop (keep containers)
docker-compose stop

# View logs after stopping
docker-compose logs avry-payments
```

## Next Steps

After successful local testing:

1. **Integration Testing**: Run with monolith (port 8000) and payments (port 3030)
2. **Create comparison script**: Verify responses are identical
3. **Test JWT validation**: When backend service is ready
4. **Test service-to-service**: Communication through Traefik gateway

## Advanced Testing

### Test with actual database (optional):

If you want to use real PostgreSQL instead of JSON storage:

1. Set up PostgreSQL:
   ```bash
   docker run --name postgres-aivery \
     -e POSTGRES_PASSWORD=password \
     -e POSTGRES_DB=aivery_payments \
     -p 5432:5432 \
     postgres:15
   ```

2. Update DATABASE_URL in .env.local:
   ```
   DATABASE_URL=postgresql://postgres:password@localhost:5432/aivery_payments
   ```

3. Restart the service:
   ```bash
   docker-compose down
   docker-compose up --build
   ```

### Test webhook handling:

```bash
curl -X POST http://localhost:3030/api/v1/payments/midtrans/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": "payment_ai_blueprint_a7f3k9m2p5q1",
    "transaction_id": "txn_123456",
    "transaction_status": "settlement",
    "fraud_status": "accept",
    "payment_type": "credit_card",
    "gross_amount": "85.00"
  }'
```

## Monitoring

### Real-time service logs:
```bash
docker-compose logs -f --tail=50 avry-payments
```

### Service metrics (if available):
```bash
docker stats avry-payments
```

### Check container status:
```bash
docker ps | grep avry-payments
```

---

## API Reference Quick Links

- **Payment Endpoints**: Implemented in `app/routes/payment.py`
- **Wallet Endpoints**: Implemented in `app/routes/wallet.py`
- **Service Models**: In `app/models/wallet.py`
- **Configuration**: `app/config.py` and `midtrans_config.py`

## Success Indicators

✅ Health endpoint returns 200 with "healthy" status  
✅ Payment config endpoint returns valid product list  
✅ Payment creation returns mock token in development mode  
✅ Wallet endpoints create and return wallet info  
✅ No errors in service logs  
✅ Service responds to requests in < 100ms (p95)  

---

For more information, see:
- [README.md](./README.md) - Service overview
- [WEEK_1_STATUS.md](./WEEK_1_STATUS.md) - Week 1 completion status
- [docs/api.md](./docs/api.md) - Detailed API documentation

