"""Isolated full-package installer acceptance; never touches the production install.

Usage: .venv\\Scripts\\python.exe scripts\\installer_qa.py --stage build\\release-... --version 18
Compiles a QA copy of installer/QQArchive-v<version>.iss with a fresh AppId and
private ConfigRoot/DesktopRoot, then checks: fresh install with the program folder
nested inside the data folder, file-by-file SHA-256 against the package, offscreen
startup, legacy desktop shortcut replacement with the versioned icon, repeat upgrade
without paths, upgrade with a pending unicode data migration in config, and an
uninstall that keeps user data and removes shortcuts. No QQ or ADB process is stopped.
"""
import argparse,hashlib,json,os,subprocess,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def digest(p):
    h=hashlib.sha256()
    with open(p,'rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

SHORTCUTS=r"""param([string]$Mode,[string]$DesktopRoot,[string]$AppRoot,[string]$Icon,[string]$Version)
$ErrorActionPreference='Stop'
$shell=New-Object -ComObject WScript.Shell
$old=Join-Path $DesktopRoot 'QQ 留档.lnk'
$new=Join-Path $DesktopRoot 'QQ聊天记录管理器.lnk'
$exe=Join-Path $AppRoot 'QQArchive.exe'
$icon=Join-Path $AppRoot $Icon
if($Mode -eq 'create'){$link=$shell.CreateShortcut($old);$link.TargetPath=$exe;$link.Save()}
elseif($Mode -eq 'verify'){
 if(Test-Path -LiteralPath $old){throw 'Legacy shortcut was not replaced'}
 if(-not(Test-Path -LiteralPath $new)){throw 'New shortcut absent'}
 $link=$shell.CreateShortcut($new)
 if($link.TargetPath -ne $exe -or -not $link.IconLocation.StartsWith($icon)){throw 'Shortcut target/icon mismatch'}
 if((Get-Item -LiteralPath $exe).VersionInfo.FileVersion -ne $Version){throw 'Wrong executable version'}
}
else{ if(Test-Path -LiteralPath $new){throw 'Shortcut left after uninstall'} }
"""

def main(argv=None):
    ap=argparse.ArgumentParser();ap.add_argument('--stage',type=Path,required=True);ap.add_argument('--version',required=True)
    ap.add_argument('--output',type=Path);a=ap.parse_args(argv)
    stage=a.stage.resolve();out=(a.output or stage/'installer-qa').resolve();out.mkdir(parents=True,exist_ok=False)
    dist=stage/'dist'/'QQArchive';numbers=(a.version.split('.')+['0','0'])[:3];file_version='.'.join(numbers)
    config=out/'config';desktop=out/'desktop';data=out/'数据';app=data/'QQArchive';pending=out/'迁移目标'
    for d in (config,desktop,data):d.mkdir()
    (data/'keep.txt').write_text('preserve user data',encoding='utf-8')
    group=f'QQArchive-v{a.version}-QA';icon=f'QQArchive-v{a.version}.ico'
    (out/'shortcuts.ps1').write_text(SHORTCUTS,encoding='utf-8-sig')
    def shortcuts(mode):
        subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(out/'shortcuts.ps1'),mode,str(desktop),str(app),icon,file_version],check=True,timeout=60)
    product='{{'+str(uuid.uuid4()).upper()+'}'
    subprocess.run([str(ROOT/'build/toolchain/inno/ISCC.exe'),'/Qp','/DProductId='+product,'/DConfigRoot='+str(config),'/DDesktopRoot='+str(desktop),
                    '/DAppSource='+str(dist),'/DOutputPath='+str(out),str(ROOT/f'installer/QQArchive-v{a.version}.iss')],check=True,timeout=600)
    setup=out/f'QQArchive-v{a.version}-Setup.exe'
    def install(label,*extra):
        proc=subprocess.run([str(setup),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-','/GROUP='+group,*extra,'/LOG='+str(out/(label+'.log'))],timeout=600)
        log=(out/(label+'.log')).read_text(encoding='utf-8-sig',errors='replace')
        assert proc.returncode==0,(label,proc.returncode,log[-1500:]);return log
    def matches():
        files=[p for p in dist.rglob('*') if p.is_file()]
        for p in files:assert digest(p)==digest(app/p.relative_to(dist)),p
        return len(files)
    results={'isolated_app_id':True}
    shortcuts('create')
    log=install('first','/DIR='+str(app),'/DATA_DIR='+str(data))
    assert 'QQArchive data directory: '+str(data) in log
    results['nested_existing_layout_allowed']=True
    results['installed_files_match_package']=matches()
    assert (app/'data-directory.txt').read_text(encoding='utf-8-sig').strip()==str(data)
    env=dict(os.environ,QT_QPA_PLATFORM='offscreen',QQARCHIVE_CONFIG_DIR=str(config),QQARCHIVE_DISABLE_TRANSFER_DETECTION='1')
    subprocess.run([str(app/'QQArchive.exe'),'--smoke'],env=env,check=True,timeout=120)
    saved=json.loads((config/'config.json').read_text(encoding='utf-8'))
    assert Path(saved['data_dir'])==data and (data/'archive.db').is_file()
    results['installed_offscreen_startup']=True
    shortcuts('verify');results['renamed_legacy_shortcut']=True;results['versioned_icon_verified']=True
    log=install('repeat')
    assert 'QQArchive program directory: '+str(app) in log and 'QQArchive data directory: '+str(data) in log
    matches();assert (data/'keep.txt').read_text(encoding='utf-8')=='preserve user data'
    results['repeat_install_without_path_selection']=True
    saved['pending_data_dir']=str(pending);text=json.dumps(saved,ensure_ascii=False);(config/'config.json').write_text(text,encoding='utf-8')
    log=install('config-upgrade')
    assert 'QQArchive data directory: '+str(pending) in log and (config/'config.json').read_text(encoding='utf-8')==text
    results['unicode_pending_migration_preserved']=True
    subprocess.run([str(app/'unins000.exe'),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/LOG='+str(out/'uninstall.log')],check=True,timeout=120)
    until=time.monotonic()+120
    while (app/'QQArchive.exe').exists() and time.monotonic()<until:time.sleep(.5)
    time.sleep(2)
    assert not (app/'QQArchive.exe').exists(),'program not removed'
    assert (data/'keep.txt').read_text(encoding='utf-8')=='preserve user data' and (data/'archive.db').is_file()
    results['uninstall_preserved_data']=True
    shortcuts('removed')
    start_menu=Path(os.environ['APPDATA'])/'Microsoft/Windows/Start Menu/Programs'/group
    assert not start_menu.exists() or not any(start_menu.iterdir()),start_menu
    results['shortcut_uninstall_cleanup']=True
    (out/'result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(results,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
