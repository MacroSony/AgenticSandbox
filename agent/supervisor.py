import subprocess
import time
import sys
import os

AGENT_SCRIPT = "loop.py"
CRASH_LOG = "crash_report.txt"

def main():
    print("SYSTEM: Supervisor initialized. Monitoring agent process...")
    
    # Ensure a barebones loop.py exists on the very first run if you haven't written one
    if not os.path.exists(AGENT_SCRIPT):
        with open(AGENT_SCRIPT, "w") as f:
            f.write("print('Hello World. I am alive.')\n")

    while True:
        print(f"\n--- SYSTEM: Booting {AGENT_SCRIPT} ---")
        
        # Open a file to capture any stderr output (like Python tracebacks)
        with open(CRASH_LOG, "w") as error_file:
            
            # We run the agent's code. 
            # -u forces unbuffered output so print() statements show up instantly in Docker logs.
            process = subprocess.Popen(
                [sys.executable, "-u", AGENT_SCRIPT], 
                stdout=sys.stdout,  # Stream normal thoughts/prints directly to console
                stderr=error_file   # Route errors to the log file
            )
            
            # Wait for the agent's loop to finish or crash
            process.wait()
        
        # Check how the agent exited
        if process.returncode != 0:
            print(f"\nSYSTEM: Agent crashed with exit code {process.returncode}.")
            
            # Read the crash log and print it to the Docker console for your visibility
            with open(CRASH_LOG, "r") as f:
                error_content = f.read()
                print(f"SYSTEM: Crash Traceback:\n{error_content}")
            
            print("SYSTEM: The traceback is saved to 'crash_report.txt'.")
            print("SYSTEM: Rebooting in 10 seconds to prevent rapid crash loops...")
            time.sleep(10)
            
        else:
            print("\nSYSTEM: Agent exited cleanly (Code 0).")
            print("SYSTEM: Rebooting in 2 seconds to apply any self-modifications...")
            time.sleep(2)

if __name__ == "__main__":
    main()