import os
import json
import subprocess

cli_exe = r'C:\Nexon\MabinogiMobile\MabinogiMobile_CLI.exe'
res = subprocess.run([cli_exe, 'capabilities'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
print("capabilities returncode:", res.returncode)
try:
    live_caps = json.loads(res.stdout)
    live_cmds = live_caps.get('commands', [])
    print(f"Total commands returned live from client: {len(live_cmds)}")
    
    # Compare with cached CAPABILITIES.json
    p = os.path.expandvars(r'%LOCALAPPDATA%\MabinogiMobileCLI\CAPABILITIES.json')
    if os.path.exists(p):
        with open(p, 'r', encoding='utf-8-sig') as f:
            cached = json.load(f).get('commands', [])
        cached_names = {c.get('Command') or c.get('command') for c in cached}
        live_names = {c.get('Command') or c.get('command') for c in live_cmds}
        
        new_cmds = live_names - cached_names
        removed_cmds = cached_names - live_names
        print("New commands since cache:", new_cmds)
        print("Removed commands:", removed_cmds)
        
    print("\nLive command list:")
    for idx, c in enumerate(live_cmds, 1):
        cmd = c.get('Command') or c.get('command')
        desc = c.get('Description') or c.get('description', '')
        print(f"{idx:2d}. {cmd:25s} | {desc}")
except Exception as e:
    print("Error parsing live output:", e)
    print("Raw output sample:", res.stdout[:500])
