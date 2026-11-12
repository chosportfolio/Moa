"""Restore official Android tools in this project, without changing the old SDK."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import shutil
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parent / 'tools'
SDK = ROOT / 'android-sdk'
GOOGLE = 'https://dl.google.com/android/repository/'
PLEOS = 'https://nexus-playground.pleos.ai/repository/raw-releases/connect-avd/2.x/crp-sys-img.xml'

def records(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        root = ET.fromstring(response.read())
    return [node for node in root.iter() if node.tag.endswith('remotePackage')]

def restore(item):
    url, target, checksum = item
    marker = target / '.cabin-installed'
    if marker.exists():
        print('Ready', target.name, flush=True)
        return
    ROOT.mkdir(parents=True, exist_ok=True)
    archive = ROOT / (target.name + '.zip')
    temp = archive.with_suffix('.download')
    if not archive.exists():
        print('Downloading', target.name, flush=True)
        digest = hashlib.sha1()
        with urllib.request.urlopen(url, timeout=120) as response, temp.open('wb') as out:
            while block := response.read(1024 * 1024):
                digest.update(block)
                out.write(block)
        if checksum and digest.hexdigest() != checksum:
            raise RuntimeError('Package checksum mismatch: ' + target.name)
        temp.replace(archive)
    print('Extracting', target.name, flush=True)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        parts = [Path(n) for n in bundle.namelist() if n and not n.endswith('/')]
        prefix = parts[0].parts[0] if all(p.parts[0] == parts[0].parts[0] for p in parts) else None
        for info in bundle.infolist():
            relative = Path(info.filename)
            if prefix:
                relative = Path(*relative.parts[1:])
            if not relative.parts or info.is_dir():
                continue
            dest = (target / relative).resolve()
            if not dest.is_relative_to(target.resolve()):
                raise ValueError('Unsafe archive entry')
            dest.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, dest.open('wb') as out:
                shutil.copyfileobj(source, out, 1024 * 1024)
    marker.write_text(url, encoding='utf-8')
    print('Installed', target, flush=True)

if __name__ == '__main__':
    chosen = {}
    for package in records(GOOGLE + 'repository2-1.xml') + records(PLEOS):
        key = package.attrib.get('path')
        dests = {'platform-tools': SDK / 'platform-tools', 'build-tools;36.0.0': SDK / 'build-tools/36.0.0',
                 'platforms;android-34': SDK / 'platforms/android-34', 'emulator': SDK / 'emulator',
                 'system-images;android-34;pleos_car_emulator;x86_64': SDK / 'system-images/android-34/pleos_car_emulator/x86_64'}
        if key not in dests or key in chosen:
            continue
        for entry in package.findall('./archives/archive'):
            if entry.findtext('host-os') not in (None, 'windows'):
                continue
            fragment = entry.findtext('./complete/url')
            url = fragment if fragment.startswith('https:') else GOOGLE + fragment
            chosen[key] = (url, dests[key], entry.findtext('./complete/checksum'))
            break
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(restore, chosen.values()))
