"""Real Inno regression: hold Windows file locks without starting an ADB server.

All installer registrations, config and files are isolated. No phone connection
is opened, no production process is stopped. Run with --output <empty directory>.
"""
import argparse
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path


def run(root, output, version='17.1'):
    output=output.resolve();output.mkdir(parents=True,exist_ok=False)
    seed=output/'seed';seed.mkdir()
    source=Path(json.loads((root/'build/latest-build.json').read_text(encoding='utf-8-sig'))['app'])
    shutil.copy2(source/'QQArchive.exe',seed/'QQArchive.exe')
    shutil.copy2(root/'qqarchive/assets/app.ico',seed/f'QQArchive-v{version}.ico')
    shutil.copytree(source/'tools',seed/'tools')
    config=output/'config';desktop=output/'desktop';app=output/'程序';data=output/'数据'
    for directory in (config,desktop,data):directory.mkdir()
    (data/'keep.txt').write_text('keep my data',encoding='utf-8')
    compiler=root/'build/toolchain/inno/ISCC.exe'
    product='{{'+str(uuid.uuid4()).upper()+'}'
    subprocess.run([str(compiler),'/Qp','/DProductId='+product,'/DConfigRoot='+str(config),
        '/DDesktopRoot='+str(desktop),'/DAppSource='+str(seed),'/DOutputPath='+str(output),
        str(root/f'installer/QQArchive-v{version}.iss')],check=True,timeout=120)
    setup=output/f'QQArchive-v{version}-Setup.exe'
    def install(label, success=True):
        proc=subprocess.run([str(setup),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-',
            '/DIR='+str(app),'/DATA_DIR='+str(data),f'/GROUP=QQArchive-v{version}-Lock-QA',
            '/LOG='+str(output/(label+'.log'))],timeout=60)
        log=(output/(label+'.log')).read_text(encoding='utf-8-sig')
        if success:assert proc.returncode==0,(label,proc.returncode,log[-1600:])
        else:
            assert proc.returncode!=0,(label,'expected preflight rejection')
            assert 'Phone component update blocked before installation' in log
            assert '-- File entry --' not in log, 'preflight must run before any installed files'
        return log
    def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
    def verify():
        for path in seed.rglob('*'):
            if path.is_file():assert digest(path)==digest(app/path.relative_to(seed)),path

    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,ctypes.c_void_p,w.DWORD,w.DWORD,w.HANDLE]
    kernel.CreateFileW.restype=w.HANDLE
    kernel.CloseHandle.argtypes=[w.HANDLE];kernel.CloseHandle.restype=w.BOOL
    def lock(path, share=1):
        handle=kernel.CreateFileW(str(path),0x80000000,share,None,3,0x80,None)
        assert handle != ctypes.c_void_p(-1).value,ctypes.get_last_error()
        return handle
    results={}
    install('fresh');verify();results['fresh_install']=True
    binaries=[app/'tools/platform-tools'/n for n in ('adb.exe','AdbWinApi.dll','AdbWinUsbApi.dll')]
    times={str(p):p.stat().st_mtime_ns for p in binaries}
    handles=[lock(p) for p in binaries]
    try:
        log=install('same-hash-locked')
        assert 'Reusing identical phone component: adb.exe' in log
        assert 'Shutting down applications using our files' not in log
        verify()
        assert all(p.stat().st_mtime_ns==times[str(p)] for p in binaries)
        results['same_hash_locked_components_reused_without_shutdown']=True
    finally:
        for handle in handles:kernel.CloseHandle(handle)
    for name in ('adb.exe','AdbWinApi.dll','AdbWinUsbApi.dll'):
        target=app/'tools/platform-tools'/name
        original=(seed/'tools/platform-tools'/name).read_bytes()
        target.write_bytes(original+b'older-tool-regression')
        program=app/'QQArchive.exe'
        program.write_bytes(b'preflight must not replace this sentinel')
        handle=lock(target)
        try:
            install('different-locked-'+name,False)
            assert program.read_bytes()==b'preflight must not replace this sentinel'
            assert target.read_bytes()==original+b'older-tool-regression'
            assert (data/'keep.txt').read_text()=='keep my data'
            results['blocked_before_writes_'+name]=True
        finally:kernel.CloseHandle(handle)
        install('unlocked-retry-'+name);verify()
    results['unlocked_updates_recover']=True
    target=binaries[0]
    handle=lock(target,0)
    try:
        install('unreadable-locked',False)
        results['unreadable_component_fails_closed']=True
    finally:kernel.CloseHandle(handle)
    binaries[2].unlink()
    install('missing-component');verify();results['missing_component_restored']=True
    subprocess.run([str(app/'unins000.exe'),'/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART'],check=True,timeout=60)
    assert (data/'keep.txt').read_text()=='keep my data'
    results['uninstall_preserves_data']=True
    (output/'result.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps(results),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--version',default='17.1')
    args=parser.parse_args()
    run(args.root,args.output,args.version)
