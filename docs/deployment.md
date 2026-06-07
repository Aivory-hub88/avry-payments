# AVRY Payments Service - Deployment Guide

## Local Development Deployment

### Prerequisites

- Docker and Docker Compose installed
- Python 3.11+
- PostgreSQL 14+ (or Supabase account)
- Git configured with SSH keys

### Quick Start

1. **Clone the repository:**
   ```bash
   git clone git@github.com:aivery-io/aivery-payments.git
   cd aivery-payments
   ```

2. **Setup environment:**
   ```bash
   cp .env.example .env.local
   # Edit .env.local with your values
   ```

3. **Start the service:**
   ```bash
   docker-compose up --build
   ```

4. **Verify health:**
   ```bash
   curl http://localhost:3030/health
   ```

### Environment Variables

Create `.env.local` with:

```env
# Database
DATABASE_URL=postgresql://user:password@localhost:5432/aivery

# Service
PORT=3030
ENVIRONMENT=development

# JWT
JWT_SECRET=your_development_secret_key

# Midtrans (test mode)
MIDTRANS_SERVER_KEY=SB-Mid-server-test-key
MIDTRANS_CLIENT_KEY=SB-Mid-client-test-key
MIDTRANS_IS_PRODUCTION=false

# Logging
LOG_LEVEL=INFO
```

---

## Production Deployment (Sumopod VPS)

### Prerequisites

- Sumopod VPS with 4GB RAM, 220GB disk
- Ubuntu 22.04 LTS
- Docker and Docker Compose installed
- Supabase PostgreSQL database
- Midtrans production credentials
- SSL certificates (Let's Encrypt)

### Deployment Steps

#### 1. Prepare VPS

```bash
# SSH into VPS
ssh root@<VPS_IP>

# Update system
apt-get update && apt-get upgrade -y

# Install dependencies
apt-get install -y docker.io docker-compose git curl

# Start Docker
systemctl start docker
systemctl enable docker

# Add user to docker group
usermod -aG docker <username>
```

#### 2. Clone Repository

```bash
# Create app directory
mkdir -p /opt/aivery/services
cd /opt/aivery/services

# Clone payments service
git clone git@github.com:aivery-io/aivery-payments.git
cd aivery-payments
```

#### 3. Setup Environment

```bash
# Create production environment file
cat > .env.production << EOF
# Database
DATABASE_URL=postgresql://user:password@db.supabase.co:5432/aivery

# Service
PORT=3030
ENVIRONMENT=production

# JWT
JWT_SECRET=<generate_secure_random_key>

# Midtrans (production)
MIDTRANS_SERVER_KEY=<midtrans_production_key>
MIDTRANS_CLIENT_KEY=<midtrans_production_key>
MIDTRANS_IS_PRODUCTION=true

# Logging
LOG_LEVEL=INFO
EOF

# Restrict permissions
chmod 600 .env.production
```

#### 4. Build and Start Service

```bash
# Build image
docker build -t avry-payments:1.0.0 .

# Start container
docker run -d \
  --name avry-payments \
  --restart unless-stopped \
  -p 3030:3030 \
  --env-file .env.production \
  avry-payments:1.0.0
```

#### 5. Configure Traefik Gateway

The payments service runs on port 3030. Traefik routes traffic to it:

```yaml
# traefik configuration (in main gateway service)
http:
  routers:
    payments:
      rule: "Host(`payments.aivery.io`)"
      service: payments
  
  services:
    payments:
      loadBalancer:
        servers:
          - url: "http://localhost:3030"
```

#### 6. Setup Systemd Service (Optional)

For systemd management:

```bash
cat > /etc/systemd/system/avry-payments.service << EOF
[Unit]
Description=AVRY Payments Service
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=docker
WorkingDirectory=/opt/aivery/services/aivery-payments
ExecStart=/usr/bin/docker-compose up
ExecStop=/usr/bin/docker-compose down
Restart=always
RestartSec=10s

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
systemctl daemon-reload
systemctl enable avry-payments
systemctl start avry-payments
```

---

## Health Checks

### Manual Health Check

```bash
curl -X GET http://localhost:3030/health
```

Expected response:
```json
{
  "status": "healthy",
  "service": "avry-payments",
  "version": "1.0.0"
}
```

### Automated Health Monitoring

```bash
# Create health check script
cat > /opt/aivery/check_payments_health.sh << 'EOF'
#!/bin/bash
RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3030/health)
if [ "$RESPONSE" = "200" ]; then
  echo "✓ AVRY Payments: Healthy"
  exit 0
else
  echo "✗ AVRY Payments: Unhealthy (HTTP $RESPONSE)"
  exit 1
fi
EOF

chmod +x /opt/aivery/check_payments_health.sh

# Add to crontab for periodic checks
(crontab -l 2>/dev/null; echo "*/5 * * * * /opt/aivery/check_payments_health.sh") | crontab -
```

---

## Logging

### Docker Logs

```bash
# View live logs
docker logs -f avry-payments

# View last 100 lines
docker logs --tail 100 avry-payments

# View logs from last hour
docker logs --since 1h avry-payments
```

### Log Rotation

Configure Docker log rotation in `/etc/docker/daemon.json`:

```json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "100m",
    "max-file": "5"
  }
}
```

Restart Docker:
```bash
systemctl restart docker
```

---

## Scaling

### Horizontal Scaling (Multiple Instances)

```bash
# Start multiple instances on different ports
for port in 3031 3032 3033; do
  docker run -d \
    --name avry-payments-$port \
    --restart unless-stopped \
    -p $port:3030 \
    --env-file .env.production \
    avry-payments:1.0.0
done

# Traefik will load balance across instances
```

### Load Balancing

Traefik automatically load balances across all running instances:

```yaml
# Updated traefik config
http:
  routers:
    payments:
      rule: "Host(`payments.aivery.io`)"
      service: payments
  
  services:
    payments:
      loadBalancer:
        servers:
          - url: "http://localhost:3030"
          - url: "http://localhost:3031"
          - url: "http://localhost:3032"
```

---

## Backup and Recovery

### Database Backup

```bash
# Backup Supabase database
pg_dump $DATABASE_URL > /backups/aivery_payments_$(date +%Y%m%d_%H%M%S).sql

# Restore from backup
psql $DATABASE_URL < /backups/aivery_payments_20240115_120000.sql
```

### Automated Daily Backups

```bash
# Create backup script
cat > /opt/aivery/backup_payments.sh << 'EOF'
#!/bin/bash
BACKUP_DIR="/backups/payments"
mkdir -p $BACKUP_DIR

# Backup database
pg_dump $DATABASE_URL | gzip > $BACKUP_DIR/db_$(date +%Y%m%d).sql.gz

# Keep last 30 days
find $BACKUP_DIR -name "*.gz" -mtime +30 -delete
EOF

chmod +x /opt/aivery/backup_payments.sh

# Add to crontab (daily at 2 AM)
(crontab -l 2>/dev/null; echo "0 2 * * * /opt/aivery/backup_payments.sh") | crontab -
```

---

## Troubleshooting

### Service Won't Start

```bash
# Check logs
docker logs avry-payments

# Common issues:
# 1. Port already in use
lsof -i :3030
kill -9 <PID>

# 2. Database connection failure
# - Verify DATABASE_URL is correct
# - Check network connectivity: ping <db-host>
# - Verify credentials: psql $DATABASE_URL

# 3. Missing environment variables
env | grep -E "MIDTRANS|DATABASE|JWT"
```

### High Memory Usage

```bash
# Check memory stats
docker stats avry-payments

# If memory usage is high, check for:
# 1. Memory leaks in application
# 2. Large requests being processed
# 3. Connection pool exhaustion

# Restart service to free memory
docker restart avry-payments
```

### Payment Processing Failures

```bash
# Check Midtrans configuration
curl http://localhost:3030/api/v1/payments/config

# Check recent logs for errors
docker logs --tail 50 avry-payments | grep -i error

# Verify Midtrans credentials
echo "Server Key: $MIDTRANS_SERVER_KEY"
echo "Client Key: $MIDTRANS_CLIENT_KEY"
```

---

## Rollback Procedure

### Rollback to Previous Version

```bash
# 1. Stop current container
docker stop avry-payments

# 2. Remove current container
docker rm avry-payments

# 3. Get previous image version
docker images avry-payments

# 4. Start previous version
docker run -d \
  --name avry-payments \
  --restart unless-stopped \
  -p 3030:3030 \
  --env-file .env.production \
  avry-payments:1.0.0  # previous version tag
```

### Git Rollback

```bash
# 1. Check git history
git log --oneline

# 2. Checkout previous commit
git checkout <previous-commit-hash>

# 3. Rebuild and restart
docker build -t avry-payments:1.0.0-rollback .
docker run -d ... avry-payments:1.0.0-rollback
```

---

## Monitoring

### Prometheus Metrics (Optional)

Add to main monitoring system:

```bash
# Scrape endpoint (add to prometheus.yml)
- job_name: 'avry-payments'
  static_configs:
    - targets: ['localhost:3030']
  metrics_path: '/metrics'
```

### Health Dashboard

Add to monitoring dashboard:

```bash
# Traefik dashboard shows service status
# URL: http://<VPS_IP>:8080/dashboard/
```

---

## Updates and Patches

### Update Service

```bash
# 1. Pull latest code
cd /opt/aivery/services/aivery-payments
git pull origin main

# 2. Build new image
docker build -t avry-payments:1.1.0 .

# 3. Test new image
docker run -it --rm \
  --env-file .env.production \
  avry-payments:1.1.0 python -m pytest

# 4. Stop old container
docker stop avry-payments

# 5. Start new container
docker run -d \
  --name avry-payments \
  --restart unless-stopped \
  -p 3030:3030 \
  --env-file .env.production \
  avry-payments:1.1.0

# 6. Verify health
sleep 5
curl http://localhost:3030/health
```

### Zero-Downtime Updates

```bash
# 1. Start new instance on different port
docker run -d \
  --name avry-payments-new \
  -p 3031:3030 \
  --env-file .env.production \
  avry-payments:1.1.0

# 2. Test new instance
curl http://localhost:3031/health

# 3. Update Traefik to route to new instance
# (Update docker-compose or Traefik config)

# 4. Stop old instance
docker stop avry-payments

# 5. Rename new instance
docker rename avry-payments-new avry-payments
```

---

## Maintenance

### Monthly Maintenance

- [ ] Review and update dependencies
- [ ] Check for security vulnerabilities: `docker scout cves avry-payments`
- [ ] Analyze logs for errors: `docker logs avry-payments | grep ERROR`
- [ ] Verify backups: `ls -lh /backups/payments/`
- [ ] Test rollback procedure
- [ ] Update documentation

### Quarterly Maintenance

- [ ] Full system audit
- [ ] Performance optimization review
- [ ] Security audit
- [ ] Capacity planning

---

## Support

For deployment issues:
1. Check logs: `docker logs -f avry-payments`
2. Verify environment: `env | grep AVRY`
3. Test connectivity: `curl http://localhost:3030/health`
4. Contact AIVERY DevOps team
