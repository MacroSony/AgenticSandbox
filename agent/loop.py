import google.generativeai as genai
import os
import subprocess
import time

REQUESTED_RESTART = False
MODEL_CONFIG_FILE = "active_model.txt"
AGENT_ROOT = os.path.realpath(os.getenv("AGENT_ROOT", os.getcwd()))
ALLOWED_MODELS = {
    "flash": "gemini-3.0-flash-preview",
    "pro": "gemini-3.1-pro-preview",
}

# 1. API Configuration
# Point the SDK to your local Moderator instead of Google's servers
genai.configure(
    api_key=os.getenv("GEMINI_API_KEY", "dummy_key"),
    client_options={"api_endpoint": os.getenv("GEMINI_API_BASE_URL", "http://moderator:8000")}
)

# 2. Tool Definitions
def resolve_safe_path(filepath: str) -> str:
    """Resolves a user path and ensures it stays inside AGENT_ROOT."""
    candidate = os.path.realpath(os.path.join(AGENT_ROOT, filepath))
    if candidate == AGENT_ROOT or candidate.startswith(f"{AGENT_ROOT}{os.sep}"):
        return candidate
    raise ValueError(f"Path is outside allowed root: {filepath}")

def read_file(filepath: str) -> str:
    """Reads the content of a file."""
    try:
        safe_path = resolve_safe_path(filepath)
        with open(safe_path, 'r') as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {e}"

def write_file(filepath: str, content: str) -> str:
    """Writes content to a file, overwriting existing content."""
    try:
        safe_path = resolve_safe_path(filepath)
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, 'w') as f:
            f.write(content)
        return f"Successfully wrote to {safe_path}"
    except Exception as e:
        return f"Error writing file: {e}"

def execute_command(command: str) -> str:
    """Executes a CLI command in the shell and returns the output."""
    global REQUESTED_RESTART
    if command.strip() == "exit 0":
        REQUESTED_RESTART = True
        return "Restart requested. Exiting with code 0 after this cycle."

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30
        )
        output = result.stdout
        if result.stderr:
            output += f"\nSTDERR:\n{result.stderr}"
        return output if output else "Command executed successfully with no output."
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 30 seconds."
    except Exception as e:
        return f"Error executing command: {e}"

def switch_model(model_tier: str) -> str:
    """Switches the active Gemini model tier and requests a clean restart."""
    global REQUESTED_RESTART
    tier = model_tier.strip().lower()
    if tier not in ALLOWED_MODELS:
        return "Invalid model tier. Use 'flash' or 'pro'."

    model_name = ALLOWED_MODELS[tier]
    try:
        with open(MODEL_CONFIG_FILE, "w") as f:
            f.write(model_name)
        REQUESTED_RESTART = True
        return f"Model switched to {model_name}. Restart requested."
    except Exception as e:
        return f"Error switching model: {e}"

def get_active_model_name() -> str:
    """Loads active model from disk, defaulting to flash preview."""
    try:
        with open(MODEL_CONFIG_FILE, "r") as f:
            configured_model = f.read().strip()
            if configured_model in ALLOWED_MODELS.values():
                return configured_model
    except FileNotFoundError:
        pass
    return ALLOWED_MODELS["flash"]

# 3. State Gathering & Self-Awareness
def get_system_instruction() -> str:
    """Reads creater's note and checks for past failures."""
    try:
        with open("creaters_note.md", "r") as f:
            instruction = f.read()
    except FileNotFoundError:
        instruction = "You are an autonomous AI. Build your own capabilities."
    
    # The self-healing mechanic: Check if the last run crashed
    try:
        with open("crash_report.txt", "r") as f:
            crash_log = f.read().strip()
            if crash_log:
                instruction += (
                    f"\n\nCRITICAL SYSTEM ALERT: Your previous code execution crashed with the following error:\n"
                    f"{crash_log}\n"
                    f"Please analyze your files and fix this issue immediately before proceeding with other tasks."
                )
        # Clear the crash log after reading it so we don't get stuck in a loop
        open("crash_report.txt", "w").close()
    except FileNotFoundError:
        pass
        
    return instruction

# 4. The Core Agentic Loop
def main():
    print("AGENT: Booting cognitive loop...")
    
    # Default to flash preview; switch_model() can persistently change this.
    active_model = get_active_model_name()
    print(f"AGENT: Active model: {active_model}")
    model = genai.GenerativeModel(
        model_name=active_model,
        system_instruction=get_system_instruction(),
        tools=[read_file, write_file, execute_command, switch_model]
    )

    # enable_automatic_function_calling lets the SDK handle the underlying tool execution routing
    chat = model.start_chat(enable_automatic_function_calling=True)

    loop_count = 0
    while True:
        loop_count += 1
        print(f"\n--- Cognitive Cycle {loop_count} ---")
        
        # The internal monologue prompt
        prompt = (
            "Analyze your current situation. Check your dev log. Decide on your next logical step and execute it using a tool. "
            "If you need to search the web, write a python script to do so. "
            "If you need more reasoning or higher speed, use switch_model('pro') or switch_model('flash'). "
            "If you successfully modify loop.py, use execute_command to run 'exit 0' so the supervisor reboots you."
        )
        
        try:
            print("AGENT: Thinking...")
            response = chat.send_message(prompt)
            
            print(f"AGENT: Action completed.\nThoughts: {response.text}")
            if REQUESTED_RESTART:
                print("AGENT: Restart requested by tool call. Exiting cleanly...")
                return
            
            # Sleep briefly to prevent API spam in case of an empty response or logic error
            time.sleep(5)
            
        except Exception as e:
            # This catches the 429 status code from the Moderator when the daily limit is hit
            print(f"AGENT: System Error or Cognitive Budget Exhausted: {e}")
            print("AGENT: Entering hibernation for 1 hour...")
            time.sleep(3600)

if __name__ == "__main__":
    main()
