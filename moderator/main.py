from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
import httpx
import os
import sqlite3
from datetime import datetime, timezone

app = FastAPI()

# Your real API key passed in via docker-compose.yml
REAL_API_KEY = os.getenv("API_KEY")
GEMINI_BASE_URL = os.getenv("API_ENDPOINT")

# Define your cognitive budget
LIMITS = {
    "pro": 100,
    "flash": 500
}

def init_db():
    """Initializes the SQLite database to track daily usage."""
    conn = sqlite3.connect("usage.db")
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

def check_and_increment_limit(model_tier: str) -> bool:
    """Checks if the agent has budget left, and increments the counter if so."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = sqlite3.connect("usage.db")
    c = conn.cursor()
    
    # Get current usage
    c.execute("SELECT calls FROM api_usage WHERE date=? AND model_tier=?", (today, model_tier))
    row = c.fetchone()
    current_calls = row[0] if row else 0
    
    # Check against limit
    max_calls = LIMITS.get(model_tier, 0)
    if current_calls >= max_calls:
        conn.close()
        return False
        
    # Increment usage
    if row:
        c.execute("UPDATE api_usage SET calls = calls + 1 WHERE date=? AND model_tier=?", (today, model_tier))
    else:
        c.execute("INSERT INTO api_usage (date, model_tier, calls) VALUES (?, ?, ?)", (today, model_tier, 1))
        
    conn.commit()
    conn.close()
    return True

# Initialize the database when the server starts
init_db()

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_to_gemini(path: str, request: Request):
    """Intercepts all requests, checks limits, and proxies to Google."""
    
    # 1. Determine the model tier from the requested path
    # Google's SDK hits paths like: /v1beta/models/gemini-1.5-pro:generateContent
    path_lower = path.lower()
    if "pro" in path_lower:
        model_tier = "pro"
    elif "flash" in path_lower:
        model_tier = "flash"
    else:
        # Default fallback if it's an embeddings model or something else
        model_tier = "flash" 

    # 2. Check the budget
    if not check_and_increment_limit(model_tier):
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

    # Read the body from the agent's request
    body = await request.body()

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
            
            # Stream the response back to the agent (handles both streaming and standard responses)
            return StreamingResponse(
                proxy_resp.aiter_raw(),
                status_code=proxy_resp.status_code,
                headers=dict(proxy_resp.headers)
            )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=f"Error connecting to Google API: {exc}")