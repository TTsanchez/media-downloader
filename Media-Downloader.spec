# -*- mode: python ; coding: utf-8 -*-
"""Сборка компактного .exe.

yt-dlp намеренно НЕ попадает в архив: при первом запуске программа скачивает
его в ``%LOCALAPPDATA%\\Media-Downloader\\tools\\pylibs``. Но чтобы скачанный
пакет работал, в архив должны попасть все зависимости, которые PyInstaller
видит при анализе установленного yt-dlp (например, stdlib-модуль ``optparse``).
Поэтому yt-dlp устанавливается в сборочное окружение, анализируется, а затем
его собственные модули вырезаются из итогового архива.
"""

from PyInstaller.utils.hooks import collect_all

datas = [('icon.ico', '.'), ('version.txt', '.')]
binaries = []
hiddenimports = []

for package in ('ttkbootstrap',):
    collected = collect_all(package)
    datas += collected[0]
    binaries += collected[1]
    hiddenimports += collected[2]

a = Analysis(
    ['main.py'],
    pathex=[],
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

# Оставляем зависимости yt-dlp, но убираем сам пакет и плагины.
DROP_ROOTS = {'yt_dlp', 'yt_dlp_ejs', 'yt_dlp_plugins'}


def _keep_module(entry):
    return entry[0].split('.')[0] not in DROP_ROOTS


def _keep_data(entry):
    return entry[0].replace('\\', '/').split('/')[0] not in DROP_ROOTS


a.pure = [entry for entry in a.pure if _keep_module(entry)]
a.datas = [entry for entry in a.datas if _keep_data(entry)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Media-Downloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
