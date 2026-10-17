#!/usr/bin/env python3
"""Build a deterministic runtime zip from an explicit allowlist."""
import hashlib
import json
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
version = json.loads((root/'.claude-plugin/plugin.json').read_text())['version']
out = root/'dist'
out.mkdir(exist_ok=True)
archive = out/f'slot-guard-{version}.zip'
files = ['.claude-plugin/icon.png', '.claude-plugin/plugin.json','.claude-plugin/marketplace.json','hooks/hooks.json',
         'hooks/register.js','guard.py','statusline.py','statusline_setup.py','LICENSE',
         'README.md','CHANGELOG.md','SECURITY.md','CONTRIBUTING.md',
         'docs/architecture.md','docs/releasing.md']
with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
    for name in sorted(files):
        entry = zipfile.ZipInfo(name, date_time=(2026,1,1,0,0,0))
        entry.compress_type = zipfile.ZIP_DEFLATED
        entry.external_attr = 0o100644 << 16
        bundle.writestr(entry, (root/name).read_bytes())
checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
(out/'SHA256SUMS').write_text(f'{checksum}  {archive.name}\n')
print(archive)
print(checksum)
