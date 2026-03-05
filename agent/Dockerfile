# agent/Dockerfile
FROM python:3.12-slim

# Install basic system dependencies the agent might want to use
RUN apt-get update && apt-get install -y curl git && rm -rf /var/lib/apt/lists/*

# Create a non-root user for security
RUN useradd -m -u 1000 agentuser
USER agentuser

WORKDIR /app

# Copy the agent's initial brain (the loop)
COPY --chown=agentuser:agentuser requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt --user

COPY --chown=agentuser:agentuser loop.py .

# The agent's loop will start here
CMD ["python", "supervisor.py"]