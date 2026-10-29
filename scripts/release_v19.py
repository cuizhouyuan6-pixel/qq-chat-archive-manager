"""Build and smoke-check in staging; publish immutable versions explicitly."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
def run(args,**kw):
    subprocess.run([str(a) for a in args],check=True,cwd=ROOT,**kw)
def digest(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def public_sources():
    """Only public Git candidates plus this version's explicitly allowed tooling."""
    output=subprocess.check_output(['git','-c','core.quotepath=false','ls-files','-z',
                                    '--cached','--others','--exclude-standard'],cwd=ROOT)
    paths={p for p in output.decode('utf-8').split('\0') if p}
    paths.update(('scripts/build_v19.ps1','scripts/release_v19.py','installer/QQArchive-v19.iss'))
    for rel in sorted(paths):
        p=(ROOT/rel).resolve()
        if not p.is_relative_to(ROOT) or not p.is_file():continue
        if p.parts[len(ROOT.parts)] not in ('qqarchive','scripts','tests','installer','docs',
                                            'main.py','requirements.txt','README.md','CHANGELOG.md','.gitignore'):
            raise ValueError('源码快照中出现非公开目录')
        yield rel,p

def complete_bundle(release,local):
    folder=local.get('artifacts_root')
    if not folder:raise ValueError('请在本机发布配置中设置 artifacts_root')
    folder=Path(folder).resolve();folder.mkdir(parents=True,exist_ok=True)
    target=folder/'QQ聊天记录管理器-v19-完整资产包.zip'
    if target.exists():raise ValueError('完整资产包已存在，不覆盖已交付文件')
    files={name:release/name for name in ('QQArchive-v19-Setup.exe','QQArchive-v19-portable.zip','源码快照.zip')}
    files.update({'README.md':ROOT/'README.md','使用与开发说明.md':ROOT/'docs/开发说明.md',
                  '更新说明.md':ROOT/'docs/releases/v19.md'})
    hashes={name:digest(p) for name,p in files.items()}
    partial=target.with_suffix('.zip.partial')
    with zipfile.ZipFile(partial,'x',zipfile.ZIP_STORED) as z:
        for name,p in files.items():z.write(p,name)
        z.writestr('SHA256.json',json.dumps(hashes,ensure_ascii=False,indent=2))
    with zipfile.ZipFile(partial) as z:
        for name,expected in hashes.items():
            h=hashlib.sha256()
            with z.open(name) as f:
                for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
            if h.hexdigest()!=expected:raise ValueError('完整资产包校验失败')
    partial.rename(target)
    (ROOT/'build/complete-asset-v19.json').write_text(json.dumps(
        {'path':str(target),'sha256':digest(target),'files':hashes},ensure_ascii=False,indent=2),encoding='utf-8')
    return target

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--compiler',default='');ap.add_argument('--build-only',action='store_true');ap.add_argument('--publish',action='store_true');ap.add_argument('--publish-github',action='store_true');ap.add_argument('--publish-existing',type=Path);args=ap.parse_args()
    release=ROOT/'测试版本'/'v19'
    if release.exists():raise RuntimeError('已发布 v19 不可覆盖；修复请使用新版本目录')
    if args.publish_existing:
        stage=args.publish_existing.resolve()
        if stage.parent != (ROOT/'build').resolve() or not stage.name.startswith('release-'):
            raise ValueError('只能发布本项目 build 下的构建目录')
        manifest=stage/'validated-artifacts.json'
        expected=json.loads(manifest.read_text(encoding='utf-8'))
        for name,sha in expected.items():
            p=(stage/name).resolve()
            if not p.is_relative_to(stage) or not p.is_file() or digest(p)!=sha:
                raise ValueError('已验收文件发生变化，必须重新验收')
        app=stage/'dist'/'QQArchive';installer=stage/'QQArchive-v19-Setup.exe'
        if set(expected) != {p.relative_to(stage).as_posix() for p in app.rglob('*') if p.is_file()} | {installer.name}:
            raise ValueError('验收清单必须覆盖全部程序文件和安装包')
        result=json.loads((stage/'exe-test'/'result.json').read_text(encoding='utf-8'))
        if not result.get('passed'):raise ValueError('EXE 自检未通过')
        publish(stage,app,installer,result,args.publish_github)
        return
    stage=ROOT/'build'/('release-'+time.strftime('%Y%m%d-%H%M%S'));stage.mkdir(parents=True)
    version_file=stage/'win-version.txt'
    version_data={'app':'QQ聊天记录管理器','version':'v19','build':'19.0.0','schema':2,'date':'2026-10-29'}
    (stage/'version.json').write_text(json.dumps(version_data,ensure_ascii=False,indent=2),encoding='utf-8')
    version_file.write_text("VSVersionInfo(ffi=FixedFileInfo(filevers=(19,0,0,0),prodvers=(19,0,0,0),mask=0x3f,flags=0x0,OS=0x40004,fileType=0x1,subtype=0x0,date=(0,0)),kids=[StringFileInfo([StringTable('080404b0',[StringStruct('FileDescription','QQ聊天记录管理器'),StringStruct('ProductName','QQ聊天记录管理器'),StringStruct('FileVersion','19.0.0'),StringStruct('ProductVersion','19.0.0')])]),VarFileInfo([VarStruct('Translation',[2052,1200])])])",encoding='utf-8')
    cmd=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--windowed','--name','QQArchive','--distpath',stage/'dist','--workpath',stage/'work','--specpath',stage,'--version-file',version_file,'--add-data',str(stage/'version.json')+';qqarchive']
    for module in ['pytest','sqlcipher3','tkinter','PySide6.QtWebEngineCore','PySide6.QtQml','PySide6.QtQuick','PySide6.QtTest']:
        cmd+=['--exclude-module',module]
    cmd += ['--icon',ROOT/'qqarchive/assets/app.ico','--add-data',str(ROOT/'qqarchive/assets')+';qqarchive/assets',ROOT/'main.py'];run(cmd)
    app=stage/'dist'/'QQArchive'
    # A versioned icon location makes Windows refresh upgraded shortcut icons.
    shutil.copy2(ROOT/'qqarchive/assets/app.ico',app/'QQArchive-v19.ico')
    # Qt 6.11 Windows imports the unversioned Windows ICU API. Some environments
    # put an unrelated ICU 78 DLL on the builder PATH; bundling it breaks QtCore.
    for name in ['icuuc.dll','icuin.dll','icudt.dll']:
        wrong=app/'_internal'/name
        if wrong.exists():wrong.unlink()
    qt=Path(importlib.metadata.distribution('PySide6_Essentials').locate_file('PySide6'))
    for runtime in qt.glob('*140*.dll'):
        shutil.copy2(runtime,app/'_internal'/runtime.name)
    # Optional local official platform-tools; never include ADB private keys or device reports.
    adb=ROOT/'tools'/'platform-tools'
    if adb.exists():
        dest=app/'tools'/'platform-tools';dest.mkdir(parents=True)
        for name in ['adb.exe','AdbWinApi.dll','AdbWinUsbApi.dll','NOTICE.txt','source.properties']:
            if (adb/name).is_file():shutil.copy2(adb/name,dest/name)
    license_dir=app/'LICENSES';license_dir.mkdir()
    for pkg in ['PySide6','PySide6_Essentials','PySide6_Addons','shiboken6','cryptography','apsw','cffi','pycparser','pyinstaller','psutil']:
        try:dist=importlib.metadata.distribution(pkg)
        except importlib.metadata.PackageNotFoundError:continue
        for f in dist.files or []:
            if any(t in f.name.lower() for t in ('license','copying','notice')) or '/licenses/' in str(f).lower():
                src=Path(dist.locate_file(f))
                if src.is_file():
                    dst=license_dir/pkg/str(f).replace('..','_');dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dst)
    (license_dir/'使用说明.txt').write_text('本程序使用 PySide6 / Qt（LGPL）、cryptography、APSW、Python、PyInstaller；许可证随附。Qt DLL 动态链接，可替换兼容版本；允许为调试这些库的修改而进行逆向工程。程序源码随版本提供。psutil 使用 BSD 3-Clause 许可证；仅采集系统计数器。ADB 为独立外部工具，只执行手机只读命令。',encoding='utf-8')
    exe=app/'QQArchive.exe';run([exe,'--self-test',stage/'exe-test'],timeout=120)
    result=json.loads((stage/'exe-test'/'result.json').read_text());assert result['passed']
    env=dict(os.environ,QT_QPA_PLATFORM='offscreen',QQARCHIVE_CONFIG_DIR=str(stage/'gui-config'))
    run([exe,'--smoke'],env=env,timeout=30)
    compiler=Path(args.compiler) if args.compiler else ROOT/'build'/'toolchain'/'inno'/'ISCC.exe'
    run([compiler,'/Qp','/DAppSource='+str(app),'/DOutputPath='+str(stage),ROOT/'installer'/'QQArchive-v19.iss'])
    installer=stage/'QQArchive-v19-Setup.exe'
    summary={'version':'v19','build':'19.0.0','staging':str(stage),'app':str(app),'installer':str(installer),'self_test':result}
    (ROOT/'build'/'latest-build.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    if args.publish:publish(stage,app,installer,result,args.publish_github)
    print(json.dumps(summary,ensure_ascii=False))

def publish(stage,app,installer,result,publish_github=False):
        release=ROOT/'测试版本'/'v19'
        if release.exists():raise RuntimeError('已发布 v19 不可覆盖')
        summary={'version':'v19','build':'19.0.0','staging':str(stage),'app':str(app),'installer':str(installer),'self_test':result}
        (ROOT/'build'/'latest-build.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        release.mkdir(parents=True)
        shutil.copytree(app,release/'QQArchive');shutil.copy2(installer,release/installer.name)
        shutil.copy2(stage/'version.json',ROOT/'qqarchive/version.json')
        for name in ['版本说明.txt','验证记录.json','审查记录.txt']:
            if (ROOT/name).is_file():shutil.copy2(ROOT/name,release/name)
        with zipfile.ZipFile(release/'源码快照.zip','w',zipfile.ZIP_DEFLATED) as z:
            for name,p in public_sources():z.write(p,name)
        with zipfile.ZipFile(release/'QQArchive-v19-portable.zip','w',zipfile.ZIP_DEFLATED) as z:
            for p in (release/'QQArchive').rglob('*'):
                if p.is_file():z.write(p,p.relative_to(release).as_posix())
        sums={p.relative_to(release).as_posix():digest(p) for p in release.rglob('*') if p.is_file()}
        (release/'SHA256.json').write_text(json.dumps(sums,ensure_ascii=False,indent=2),encoding='utf-8')
        index_path=release.parent/'版本索引.json'
        previous=json.loads(index_path.read_text(encoding='utf-8-sig')) if index_path.exists() else {}
        # Machine-specific settings (history folder, privacy patterns) live in the
        # untracked .local/release.json so the public repository holds no local paths.
        local_file=ROOT/'.local'/'release.json';local=json.loads(local_file.read_text(encoding='utf-8')) if local_file.is_file() else {}
        bundle=complete_bundle(release,local)
        (release.parent/'版本索引.json').write_text(json.dumps({'history':previous.get('history',[]),'current':'v19','directory':'v19','installer':'v19/QQArchive-v19-Setup.exe','history_root':local.get('history_root','')},ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'published':str(release),'installer':str(release/installer.name),'complete_asset':str(bundle)},ensure_ascii=False))
        if not publish_github:return
        # Optional public release, after the existing privacy gate.
        github=subprocess.run([sys.executable,str(ROOT/'scripts'/'github_publish.py'),'--version',release.name[1:]],cwd=ROOT)
        if github.returncode:
            print(f'本地发布已完成，GitHub 发布未完成；处理后运行 scripts/github_publish.py --version {release.name[1:]}');sys.exit(3)
if __name__=='__main__':main()

