# avry-payments

Payment processing service for the Aivory platform — Midtrans integration, subscriptions, and credits.

## Tech Stack

- Python 3.11+
- FastAPI + Uvicorn
- PostgreSQL
- Midtrans Payment Gateway
- Docker

## Directory Structure

```
avry-payments/
├── app/            # Application source code
├── data/           # Payment data / logs
├── docs/           # API documentation
├── migrations/     # Database migrations
├── main.py         # Entry point
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Run Locally

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --host 0.0.0.0 --port 8085 --reload
```

## Docker

```bash
docker compose up --build
```

## VPS Deployment

```bash
docker compose -f docker-compose.yml up -d --build
```

Ensure `.env` is configured on the server with production Midtrans credentials.

## Part of Aivory

This service is part of the [Aivory platform](https://github.com/ClementHansel/aivory).
