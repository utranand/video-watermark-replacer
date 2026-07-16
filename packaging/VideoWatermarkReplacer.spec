# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import copy_metadata

datas = [('/Users/puttipongu/Library/CloudStorage/GoogleDrive-utranand@gmail.com/My Drive/workspace/src/github/sidekicks/projects/gdrive-sk/services/video-watermark-remover/src/static', 'static'), ('/Users/puttipongu/Library/CloudStorage/GoogleDrive-utranand@gmail.com/My Drive/workspace/src/github/sidekicks/projects/gdrive-sk/services/video-watermark-remover/resources/paper-sticker.png', 'resources')]
binaries = []
hiddenimports = ['wmr']
datas += copy_metadata('imageio')
hiddenimports += collect_submodules('imageio.plugins')
tmp_ret = collect_all('imageio_ffmpeg')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['/Users/puttipongu/Library/CloudStorage/GoogleDrive-utranand@gmail.com/My Drive/workspace/src/github/sidekicks/projects/gdrive-sk/services/video-watermark-remover/src/app.py'],
    pathex=['/Users/puttipongu/Library/CloudStorage/GoogleDrive-utranand@gmail.com/My Drive/workspace/src/github/sidekicks/projects/gdrive-sk/services/video-watermark-remover/src'],
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
    [],
    exclude_binaries=True,
    name='VideoWatermarkReplacer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='VideoWatermarkReplacer',
)
app = BUNDLE(
    coll,
    name='VideoWatermarkReplacer.app',
    icon=None,
    bundle_identifier=None,
)
