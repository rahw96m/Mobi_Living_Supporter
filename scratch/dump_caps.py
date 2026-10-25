import os
import json

p = os.path.expandvars(r'%LOCALAPPDATA%\MabinogiMobileCLI\CAPABILITIES.json')
with open(p, 'r', encoding='utf-8-sig') as f:
    data = json.load(f)

for c in data.get('commands', []):
    cmd = c.get('Command') or c.get('command')
    desc = c.get('Description') or c.get('description')
    b_ex = c.get('BodyExample')
    out_ex = c.get('OutputExample')
    meta = c.get('Metadata', {})
    note = c.get('Note') or meta.get('note') or ''
    confirm = meta.get('requiresConfirm', False)
    cost = meta.get('cost') or ''
    print(f"=== {cmd} ===")
    print(f"Description: {desc}")
    if note:
        print(f"Note: {note}")
    if confirm:
        print(f"Requires Confirm: {confirm}")
    if cost:
        print(f"Cost: {cost}")
    if b_ex is not None:
        print(f"Body Example: {b_ex}")
    print()
