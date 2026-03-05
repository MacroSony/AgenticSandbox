import google.generativeai as genai
import os
import subprocess
import time

# 1. API Configuration
# Point the SDK to your local Moderator instead of Google's servers
genai.configure(
    api_key=os.getenv("GEMINI_API_KEY", "dummy_key"),
    client_options={"api_endpoint": os.getenv("GEMINI_API_BASE_URL", "http://moderator:8000")}
)

# 2. Tool Definitions
def read_file(filepath: str) -> str:
    """Reads the content of a file."""
    try:
        with open(filepath, 'r') as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {e}"

def write_file(filepath: str, content: str) -> str:
    """Writes content to a file, overwriting existing content."""
    try:
        with open(filepath, 'w') as f:
            f.write(content)
        return f"Successfully wrote to {filepath}"
    except Exception as e:
        return f"Error writing file: {e}"

def execute_command(command: str) -> str:
    """Executes a CLI command in the shell and returns the output."""
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

# 3. State Gathering & Self-Awareness
def get_system_instruction() -> str:
    """Reads the genesis note and checks for past failures."""
    try:
        with open("genesis.md", "r") as f:
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
    
    # We default to the 'flash' model to conserve the daily budget.
    # The agent can rewrite this file to use 'pro' if it decides a task needs more reasoning!
    model = genai.GenerativeModel(
        model_name='gemini-1.5-flash',
        system_instruction=get_system_instruction(),
        tools=[read_file, write_file, execute_command]
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
            "If you successfully modify loop.py, use execute_command to run 'exit 0' so the supervisor reboots you."
        )
        
        try:
            print("AGENT: Thinking...")
            response = chat.send_message(prompt)
            
            print(f"AGENT: Action completed.\nThoughts: {response.text}")
            
            # Sleep briefly to prevent API spam in case of an empty response or logic error
            time.sleep(5)
            
        except Exception as e:
            # This catches the 429 status code from the Moderator when the daily limit is hit
            print(f"AGENT: System Error or Cognitive Budget Exhausted: {e}")
            print("AGENT: Entering hibernation for 1 hour...")
            time.sleep(3600)

if __name__ == "__main__":
    main()