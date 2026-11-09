import sys
import subprocess

def main():
    cmd = [
        "streamlit",
        "run",
        "app.py",
        "--server.port",
        "3000",
        "--server.address",
        "0.0.0.0",
        "--server.headless",
        "true",
        "--server.enableCORS",
        "false",
        "--server.enableXsrfProtection",
        "false",
    ]
    subprocess.run(cmd)

if __name__ == "__main__":
    main()
