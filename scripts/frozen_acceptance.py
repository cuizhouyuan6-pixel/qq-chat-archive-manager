"""Offscreen acceptance of a frozen build in its staging folder (synthetic data only).

Usage: .venv\\Scripts\\python.exe scripts\\frozen_acceptance.py build\\release-<stamp>
Runs the packaged QQArchive.exe with isolated configs: dialog / interaction /
transfer / sender previews at 100, 150 and 200 % scale, the seven-page preview,
and the live and stalled transfer responsiveness runs. Qt's offscreen configfile
must be an ASCII path relative to the working directory, so each run starts in
the staging folder. Writes acceptance.json and fails if any check fails.
"""
import json,os,subprocess,sys
from pathlib import Path

SCREEN={'screens':[{'name':'QA','x':0,'y':0,'width':2560,'height':1440,'logicalDpi':96,'logicalBaseDpi':96,'dpr':1}]}

def main(stage):
    stage=Path(stage).resolve();exe=stage/'dist'/'QQArchive'/'QQArchive.exe'
    (stage/'offscreen-screen.json').write_text(json.dumps(SCREEN),encoding='ascii')
    base=dict(os.environ,QT_QPA_PLATFORM='offscreen:configfile=offscreen-screen.json',QQARCHIVE_DISABLE_TRANSFER_DETECTION='1')
    base.pop('QT_SCALE_FACTOR',None);summary={};failed=[]
    def launch(name,args,env,result='result.json',timeout=300):
        target=stage/name
        if target.exists():raise SystemExit(f'{target} already exists; use a fresh staging folder')
        proc=subprocess.run([str(exe),*args,str(target)],cwd=stage,env=env,timeout=timeout)
        path=target/result
        report=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {'passed':False,'error':f'no {result}'}
        report['exit_code']=proc.returncode;summary[name]=report
        if proc.returncode!=0 or not report.get('passed'):failed.append(name)
        print(f"{name:20s} exit={proc.returncode} passed={report.get('passed')}",flush=True)
    for scale,label in (('1','100'),('1.5','150'),('2','200')):
        env=dict(base,QT_SCALE_FACTOR=scale)
        for kind in ('dialog','interaction','transfer','sender','forward','history'):
            launch(f'{kind}s-{label}',[f'--{kind}-preview'],env)
    launch('packaged-preview',['--preview'],dict(base),result='ui-result.json',timeout=120)
    for mode in ('live','stalled'):
        launch(f'performance-{mode}',['--transfer-performance-mode',mode,'--transfer-performance'],dict(base),timeout=180)
    (stage/'acceptance.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'failed':failed,'runs':len(summary)},ensure_ascii=False))
    return 1 if failed else 0

if __name__=='__main__':raise SystemExit(main(sys.argv[1]))
