import subprocess
import sys
import os

script_path = "main.py"
icon_path = "icon.png"

if not os.path.exists(script_path):
    print(f"Erreur: {script_path} introuvable")
    sys.exit(1)

if not os.path.exists(icon_path):
    print(f"Erreur: {icon_path} introuvable")
    sys.exit(1)

command = [
    sys.executable,
    "-m",
    "PyInstaller",
    "--onefile",
    "--noconsole",
    f"--icon={icon_path}",
    script_path
]

subprocess.run(command, check=True)