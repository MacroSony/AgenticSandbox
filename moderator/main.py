from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from starlette.background import BackgroundTask
import httpx
import os
import sqlite3
from datetime import datetime, timezone
import re
import time

app = FastAPI()

# Your real API key passed in via docker-compose.yml
REAL_API_KEY = os.getenv("API_KEY")
GEMINI_BASE_URL = os.getenv("API_ENDPOINT")
AGENT_TOKEN = os.getenv("AGENT_TOKEN")

# Define your cognitive budget
LIMITS = {
    "pro": 100,
    "flash": 500
}

def validate_config():
    """Fail fast if required runtime configuration is missing."""
    missing = []
    if not REAL_API_KEY:
        missing.append("API_KEY")
    if not GEMINI_BASE_URL:
        missing.append("API_ENDPOINT")
    if not AGENT_TOKEN:
        missing.append("AGENT_TOKEN")
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(f"Missing required environment variable(s): {names}")

def open_db():
    conn = sqlite3.connect("usage.db", timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def init_db():
    """Initializes the SQLite database to track daily usage."""
    conn = open_db()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS api_usage (
            date TEXT,
            model_tier TEXT,
            calls INTEGER,
            PRIMARY KEY (date, model_tier)
        )
    ''')
    conn.commit()
    conn.close()

def with_retries(fn, retries: int = 3, delay_sec: float = 0.05):
    """Retries sqlite operations that can fail under contention."""
    for attempt in range(retries):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == retries - 1:
                raise
            time.sleep(delay_sec * (attempt + 1))

def reserve_budget(model_tier: str) -> bool:
    """Atomically reserves one call for this tier if budget remains."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    max_calls = LIMITS[model_tier]

    def _op():
        conn = open_db()
        try:
            c = conn.cursor()
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "SELECT calls FROM api_usage WHERE date=? AND model_tier=?",
                (today, model_tier)
            )
            row = c.fetchone()
            current_calls = row[0] if row else 0
            if current_calls >= max_calls:
                conn.rollback()
                return False

            if row:
                c.execute(
                    "UPDATE api_usage SET calls = calls + 1 WHERE date=? AND model_tier=?",
                    (today, model_tier)
                )
            else:
                c.execute(
                    "INSERT INTO api_usage (date, model_tier, calls) VALUES (?, ?, ?)",
                    (today, model_tier, 1)
                )
            conn.commit()
            return True
        finally:
            conn.close()

    return with_retries(_op)

def refund_budget(model_tier: str) -> None:
    """Refunds one reserved call if upstream request failed."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    def _op():
        conn = open_db()
        try:
            c = conn.cursor()
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                """
                UPDATE api_usage
                SET calls = CASE WHEN calls > 0 THEN calls - 1 ELSE 0 END
                WHERE date=? AND model_tier=?
                """,
                (today, model_tier)
            )
            conn.commit()
        finally:
            conn.close()
    with_retries(_op)

def infer_model_tier(path: str, body: bytes) -> str | None:
    """Infers billing tier from request model; unknown models are denied."""
    path_lower = path.lower()
    model_name = None

    path_match = re.search(r"models/([^:/?]+)", path_lower)
    if path_match:
        model_name = path_match.group(1)
    else:
        body_text = body.decode("utf-8", errors="ignore").lower()
        body_match = re.search(r'"model"\s*:\s*"([^"]+)"', body_text)
        if body_match:
            model_name = body_match.group(1).split("/")[-1]

    if not model_name:
        return None
    if "pro" in model_name:
        return "pro"
    if "flash" in model_name:
        return "flash"
    return None

# Initialize the database when the server starts
validate_config()
init_db()

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_to_gemini(path: str, request: Request):
    """Intercepts all requests, checks limits, and proxies to Google."""

    if request.headers.get("x-goog-api-key") != AGENT_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized agent token.")

    # Read the body once; reuse for tier inference and proxying.
    body = await request.body()

    # 1. Determine the model tier from the requested path
    # Google's SDK hits paths like: /v1beta/models/gemini-1.5-pro:generateContent
    model_tier = infer_model_tier(path, body)
    if model_tier not in LIMITS:
        raise HTTPException(status_code=400, detail="Unknown or unsupported model tier.")

    # 2. Check the budget
    if not reserve_budget(model_tier):
        # Return a custom error that the agent can read and understand
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "message": f"SYSTEM OVERRIDE: Daily limit of {LIMITS[model_tier]} calls for '{model_tier}' model reached. Please sleep or switch models.",
                    "status": "RESOURCE_EXHAUSTED"
                }
            }
        )

    # 3. Forward the request to the real Gemini API
    url = f"{GEMINI_BASE_URL}/{path}"
    
    # The Google SDK appends the dummy API key to the header (x-goog-api-key). 
    # We strip it out and inject our REAL key.
    headers = dict(request.headers)
    headers.pop("host", None) 
    headers["x-goog-api-key"] = REAL_API_KEY

    # Proxy the request using httpx
    async with httpx.AsyncClient() as client:
        try:
            proxy_req = client.build_request(
                method=request.method,
                url=url,
                headers=headers,
                content=body,
                params=request.query_params
            )
            proxy_resp = await client.send(proxy_req, stream=True)

            # Refund reservation when upstream fails.
            if not (200 <= proxy_resp.status_code < 400):
                refund_budget(model_tier)
            
            # Stream the response back to the agent (handles both streaming and standard responses)
            return StreamingResponse(
                proxy_resp.aiter_raw(),
                status_code=proxy_resp.status_code,
                headers=dict(proxy_resp.headers),
                background=BackgroundTask(proxy_resp.aclose)
            )
        except httpx.RequestError as exc:
            refund_budget(model_tier)
            raise HTTPException(status_code=502, detail=f"Error connecting to Google API: {exc}")
        except Exception:
            refund_budget(model_tier)
            raise
