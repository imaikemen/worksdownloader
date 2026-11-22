# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

datas = [('index.html', '.'), ('licenses', 'licenses')]
if Path('runtime').is_dir():
    datas.append(('runtime', 'runtime'))

for file in Path('core').rglob('*'):
    if file.is_file() and '__pycache__' not in file.parts and not file.name.endswith(('.pyc', '.bak.v1')):
        datas.append((str(file), str(file.parent)))

binaries = []
hiddenimports = [
    'PIL.Image', 'PIL.JpegImagePlugin', 'PIL.PngImagePlugin',
    'PIL.WebPImagePlugin', 'PIL.AvifImagePlugin', 'PIL.GifImagePlugin',
    'configparser', 'sqlite3', 'ipaddress', 'mimetypes', 'http.cookiejar',
    'html.parser', 'html', 'argparse', 'tempfile', 'uuid', 'shutil',
    'socket', 'ssl', 'datetime', 'urllib.request', 'urllib.parse', 'urllib.error',
]

tmp_ret = collect_all('yt_dlp')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

desktop = collect_all('webview')
datas += desktop[0]
binaries += desktop[1]
hiddenimports += desktop[2]

pathex = ['.']
if Path('core/vendor/transparent-core').is_dir():
    pathex.append('core/vendor/transparent-core')

a = Analysis(
    ['server.py'],
    pathex=pathex,
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='作品鉴赏downloader-V1.0.1',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
