import os
import json
import subprocess

p = os.path.expandvars(r'%LOCALAPPDATA%\MabinogiMobileCLI\CAPABILITIES.json')
if os.path.exists(p):
    with open(p, 'r', encoding='utf-8-sig') as f:
        data = json.load(f)
    cmds = data.get('commands', [])
    print(f"Total commands in local CAPABILITIES.json: {len(cmds)}")
    if cmds:
        print("Sample keys of first command:", list(cmds[0].keys()))
    for idx, c in enumerate(cmds, 1):
        cmd = c.get('Command') or c.get('command') or c.get('name')
        desc = c.get('Description') or c.get('description', '')
        meta = c.get('Metadata') or c.get('metadata', {})
        req_confirm = meta.get('requiresConfirm', False)
        print(f"{idx:2d}. {str(cmd):25s} | {desc} (confirm: {req_confirm})")

cli_exe = r'C:\Nexon\MabinogiMobile\MabinogiMobile_CLI.exe'
print("\nChecking MabinogiMobile_CLI status...")
try:
    res = subprocess.run([cli_exe, 'status'], capture_output=True, text=True, timeout=5)
    print("status returncode:", res.returncode)
    print("status stdout:", res.stdout.strip())
    print("status stderr:", res.stderr.strip())
except Exception as e:
    print("status error:", e)
