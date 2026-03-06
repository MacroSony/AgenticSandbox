from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, Response
from starlette.background import BackgroundTask
import httpx
import os
import sqlite3
from datetime import datetime, timezone
import re
import time
from urllib.parse import urljoin

app = FastAPI()

# Your real API key passed in via docker-compose.yml
REAL_API_KEY = os.getenv("API_KEY")
GEMINI_BASE_URL = os.getenv("API_ENDPOINT")
AGENT_TOKEN = os.getenv("AGENT_TOKEN")

# Define your cognitive budget
LIMITS = {
    "pro": 200,
    "flash": 800
}

def parse_timeout_env(name: str, default: float, allow_none: bool = False) -> float | None:
    """
    Parses timeout env vars safely.
    If allow_none=True, values <= 0 disable the timeout (None).
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        log_event(f"invalid timeout env {name}={raw!r}; using default={default}")
        return default

    if allow_none and value <= 0:
        return None
    if value <= 0:
        log_event(f"invalid timeout env {name}={raw!r}; must be > 0; using default={default}")
        return default
    return value

def build_upstream_timeout() -> httpx.Timeout:
    """Creates an upstream timeout profile suitable for longer model inference."""
    connect_timeout = parse_timeout_env("UPSTREAM_CONNECT_TIMEOUT_SECONDS", 5.0)
    read_timeout = parse_timeout_env("UPSTREAM_READ_TIMEOUT_SECONDS", 900.0, allow_none=True)
    write_timeout = parse_timeout_env("UPSTREAM_WRITE_TIMEOUT_SECONDS", 60.0)
    pool_timeout = parse_timeout_env("UPSTREAM_POOL_TIMEOUT_SECONDS", 60.0)
    return httpx.Timeout(
        connect=connect_timeout,
        read=read_timeout,
        write=write_timeout,
        pool=pool_timeout,
    )

def log_event(message: str) -> None:
    """Simple stdout logging for container-level tracing."""
    timestamp = datetime.now(timezone.utc).isoformat()
    print(f"[MODERATOR {timestamp}] {message}", flush=True)

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

async def probe_upstream_connectivity() -> None:
    """Checks if the upstream endpoint is reachable from inside the moderator container."""
    timeout = httpx.Timeout(timeout=8.0, connect=3.0, read=8.0, write=8.0, pool=8.0)
    probe_path = "/v1/models"
    probe_url = urljoin(f"{GEMINI_BASE_URL.rstrip('/')}/", probe_path.lstrip("/"))
    probe_headers = {
        "x-goog-api-key": REAL_API_KEY,
        "x-api-key": REAL_API_KEY,
        "authorization": f"Bearer {REAL_API_KEY}",
    }
    probe_params = {"key": REAL_API_KEY}

    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        try:
            response = await client.get(probe_url, headers=probe_headers, params=probe_params)
            log_event(
                f"startup upstream probe: reachable url={probe_url} status={response.status_code}"
            )
            if not (200 <= response.status_code < 500):
                log_event(
                    f"startup upstream probe warning: unexpected-status status={response.status_code}"
                )
        except httpx.RequestError as exc:
            exc_type = type(exc).__name__
            log_event(
                f"startup upstream probe: UNREACHABLE url={probe_url} type={exc_type} error={repr(exc)}"
            )

def should_stream_response(path: str) -> bool:
    """
    Stream only for explicit streaming APIs.
    Regular :generateContent should be buffered to avoid mid-stream relay errors.
    """
    path_lower = path.lower()
    return ":streamgeneratecontent" in path_lower

def open_db():
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect("data/usage.db", timeout=5.0)
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
log_event(f"startup complete; upstream endpoint={GEMINI_BASE_URL}")
log_event(
    "upstream timeout profile: "
    f"connect={parse_timeout_env('UPSTREAM_CONNECT_TIMEOUT_SECONDS', 5.0)}s "
    f"read={parse_timeout_env('UPSTREAM_READ_TIMEOUT_SECONDS', 900.0, allow_none=True)}s "
    f"write={parse_timeout_env('UPSTREAM_WRITE_TIMEOUT_SECONDS', 60.0)}s "
    f"pool={parse_timeout_env('UPSTREAM_POOL_TIMEOUT_SECONDS', 60.0)}s"
)

@app.on_event("startup")
async def startup_probe() -> None:
    await probe_upstream_connectivity()

@app.get("/usage")
async def get_usage(request: Request):
    """Returns the current API usage for the day."""
    incoming_key = request.headers.get("x-goog-api-key") or request.query_params.get("key")
    if incoming_key != AGENT_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized agent token.")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    usage = {"date": today, "pro": 0, "flash": 0, "limits": LIMITS}
    
    def _op():
        conn = open_db()
        try:
            c = conn.cursor()
            c.execute("SELECT model_tier, calls FROM api_usage WHERE date=?", (today,))
            for tier, calls in c.fetchall():
                if tier in usage:
                    usage[tier] = calls
            return usage
        finally:
            conn.close()

    return with_retries(_op)

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_to_gemini(path: str, request: Request):
    """Intercepts all requests, checks limits, and proxies to Google."""

    incoming_key = request.headers.get("x-goog-api-key") or request.query_params.get("key")
    if incoming_key != AGENT_TOKEN:
        log_event(
            f"incoming request rejected: method={request.method} path=/{path} reason=unauthorized-agent-token"
        )
        raise HTTPException(status_code=401, detail="Unauthorized agent token.")

    # Read the body once; reuse for tier inference and proxying.
    body = await request.body()
    log_event(
        f"incoming request accepted: method={request.method} path=/{path} bytes={len(body)} client={request.client}"
    )

    # 1. Determine the model tier from the requested path
    # Google's SDK hits paths like: /v1beta/models/gemini-1.5-pro:generateContent
    model_tier = infer_model_tier(path, body)
    if model_tier not in LIMITS:
        log_event(f"incoming request rejected: path=/{path} reason=unknown-model-tier")
        raise HTTPException(status_code=400, detail="Unknown or unsupported model tier.")

    # 2. Check the budget
    if not reserve_budget(model_tier):
        log_event(f"budget exceeded: tier={model_tier} path=/{path}")
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
    log_event(
        f"forwarding upstream: method={request.method} tier={model_tier} url={url}"
    )
    
    # The Google SDK appends the dummy API key to the header (x-goog-api-key). 
    # We strip it out and inject our REAL key.
    headers = dict(request.headers)
    headers.pop("host", None) 
    headers["x-goog-api-key"] = REAL_API_KEY
    headers["x-api-key"] = REAL_API_KEY
    headers["authorization"] = f"Bearer {REAL_API_KEY}"
    upstream_params = dict(request.query_params)
    upstream_params.pop("auth_token", None)
    upstream_params["key"] = REAL_API_KEY

    # Proxy the request using httpx
    timeout = build_upstream_timeout()
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        try:
            stream_upstream = should_stream_response(path)
            proxy_req = client.build_request(
                method=request.method,
                url=url,
                headers=headers,
                content=body,
                params=upstream_params
            )
            proxy_resp = await client.send(proxy_req, stream=stream_upstream)
            log_event(
                f"upstream response: method={request.method} path=/{path} status={proxy_resp.status_code}"
            )

            # Refund reservation when upstream fails.
            if not (200 <= proxy_resp.status_code < 400):
                refund_budget(model_tier)
                error_body = await proxy_resp.aread()
                error_preview = error_body.decode("utf-8", errors="replace")[:500]
                log_event(
                    f"upstream error body: method={request.method} path=/{path} status={proxy_resp.status_code} body={error_preview!r}"
                )
                headers = dict(proxy_resp.headers)
                headers.pop("transfer-encoding", None)
                await proxy_resp.aclose()
                return Response(
                    content=error_body,
                    status_code=proxy_resp.status_code,
                    headers=headers,
                )

            if stream_upstream:
                async def iter_upstream():
                    try:
                        async for chunk in proxy_resp.aiter_raw():
                            yield chunk
                    except (httpx.ReadError, httpx.RemoteProtocolError) as exc:
                        log_event(
                            f"upstream stream read error: method={request.method} path=/{path} type={type(exc).__name__} error={repr(exc)}"
                        )
                    finally:
                        await proxy_resp.aclose()

                return StreamingResponse(
                    iter_upstream(),
                    status_code=proxy_resp.status_code,
                    headers=dict(proxy_resp.headers),
                )

            # Non-stream responses are buffered to completion before returning.
            response_body = await proxy_resp.aread()
            response_headers = dict(proxy_resp.headers)
            response_headers.pop("transfer-encoding", None)
            await proxy_resp.aclose()
            return Response(
                content=response_body,
                status_code=proxy_resp.status_code,
                headers=response_headers,
            )
        except httpx.RequestError as exc:
            refund_budget(model_tier)
            exc_type = type(exc).__name__
            log_event(
                f"upstream connection error: method={request.method} path=/{path} type={exc_type} error={repr(exc)}"
            )
            raise HTTPException(status_code=502, detail=f"Error connecting to upstream API ({exc_type}): {exc}")
        except Exception:
            refund_budget(model_tier)
            log_event(f"upstream unexpected error: method={request.method} path=/{path}")
            raise
