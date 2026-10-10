"""Commit, tag and publish one released version to GitHub behind a privacy gate.

Usage: .venv\\Scripts\\python.exe scripts\\github_publish.py --version 18.1 [--dry-run]

Needs git and gh on PATH, a one-time `gh auth login`, and this folder as a git
repository whose origin is the GitHub repository. It publishes:
  * files allowed by .gitignore, plus this version's build/release/installer scripts;
  * docs/releases/v<version>.md, the public update report. Only "## 新功能" and
    "## 修复" sections are allowed. It is prepended to CHANGELOG.md and used as the
    GitHub Release notes;
  * the installer and portable zip of the published version as release assets.
Every staged file, the notes and the unpacked program files are scanned for secrets,
user-profile paths and the private patterns kept only in the untracked
.local/release.json. Any hit, or a commit e-mail other than GitHub's noreply
address, stops the run before anything leaves this computer.
"""
import argparse,json,re,subprocess,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
GENERIC=[re.compile(r'[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s"\']+'),re.compile(r'BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY'),
         re.compile(r'\badbkey\b'),re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}')]
SECTIONS={'新功能','修复'}

def git(*args,check=True):
    p=subprocess.run(['git','-c','core.quotepath=false',*args],cwd=ROOT,capture_output=True,text=True,encoding='utf-8')
    if check and p.returncode:raise SystemExit('git '+' '.join(args[:2])+' failed: '+(p.stderr or p.stdout).strip())
    return p.stdout.strip()

def gh(*args,check=True):
    p=subprocess.run(['gh',*args],cwd=ROOT,capture_output=True,text=True,encoding='utf-8')
    if check and p.returncode:raise SystemExit('gh '+' '.join(args[:2])+' failed: '+(p.stderr or p.stdout).strip())
    return p

def notes_for(tag):
    path=ROOT/'docs'/'releases'/f'{tag}.md';text=path.read_text(encoding='utf-8');lines=text.splitlines()
    if not lines or not re.match(rf'# {re.escape(tag)}(?:[（( ]|$)',lines[0]):raise SystemExit(f'{path.name}: first line must be "# {tag}…"')
    found=[l[3:].strip() for l in lines if l.startswith('## ')]
    if not found or set(found)-SECTIONS:raise SystemExit(f'{path.name}: only "## 新功能" / "## 修复" sections are allowed, found {found}')
    return path,text

def scan_text(text,patterns,label):
    hits=[]
    for s in patterns:
        hits+=[f'{label}:{text.count(chr(10),0,m.start())+1}: private pattern' for m in re.finditer(re.escape(s),text)]
    for g in GENERIC:
        hits+=[f'{label}:{text.count(chr(10),0,m.start())+1}: {m.group(0)[:24]}…' for m in g.finditer(text)]
    return hits

def scan_bytes(data,patterns,label):
    needles=[s.encode(enc) for s in patterns for enc in ('utf-8','utf-16-le')]
    return [f'{label}: private pattern'] if any(n in data for n in needles) else []

def changelog_with(notes,tag):
    path=ROOT/'CHANGELOG.md';old=path.read_text(encoding='utf-8') if path.exists() else '# 更新记录\n'
    if re.search(rf'^## {re.escape(tag)}(?:[（( ]|$)',old,re.M):return path,old
    entry='\n'.join(('#'+l) if l.startswith('#') else l for l in notes.strip().splitlines())
    head,_,rest=old.partition('\n')
    return path,head+'\n\n'+entry+'\n'+(('\n'+rest.lstrip('\n')) if rest.strip() else '')

def main(argv=None):
    ap=argparse.ArgumentParser();ap.add_argument('--version',required=True);ap.add_argument('--dry-run',action='store_true');a=ap.parse_args(argv)
    tag='v'+a.version.lstrip('v');local=json.loads((ROOT/'.local'/'release.json').read_text(encoding='utf-8'));patterns=local.get('private_patterns',[])
    release=ROOT/'测试版本'/tag;assets=[release/f'QQArchive-{tag}-Setup.exe',release/f'QQArchive-{tag}-portable.zip']
    if not all(p.is_file() for p in assets):raise SystemExit(f'{tag} is not a published version with installer and portable zip')
    if not git('config','user.email').endswith('@users.noreply.github.com'):raise SystemExit('commit e-mail must be the GitHub noreply address')
    notes_path,notes=notes_for(tag);log_path,log=changelog_with(notes,tag)
    hits=scan_text(notes,patterns,notes_path.name)+scan_text(log,patterns,'CHANGELOG.md')
    for p in (release/'QQArchive').rglob('*'):
        if p.is_file():hits+=scan_bytes(p.read_bytes(),patterns,f'{tag}/QQArchive/{p.relative_to(release/"QQArchive").as_posix()}')
    if hits:raise SystemExit('privacy gate stopped the release:\n'+'\n'.join(hits[:50]))
    if not a.dry_run:log_path.write_text(log,encoding='utf-8')
    stem=a.version.lstrip('v').replace('.','_')
    for rel in (f'scripts/build_v{stem}.ps1',f'scripts/release_v{stem}.py',f'installer/QQArchive-{tag}.iss'):
        if (ROOT/rel).is_file():git('add','-f',rel)
    git('add','-A');staged=[s for s in git('diff','--cached','--name-only','--diff-filter=ACMR').splitlines() if s]
    for rel in staged:
        data=(ROOT/rel).read_bytes()
        try:hits+=scan_text(data.decode('utf-8'),patterns,rel)
        except UnicodeDecodeError:hits+=scan_bytes(data,patterns,rel)
    if hits or a.dry_run:
        git('reset','-q')
        if hits:raise SystemExit('privacy gate stopped the commit:\n'+'\n'.join(hits[:50]))
        print(json.dumps({'dry_run':True,'tag':tag,'staged':len(staged),'assets':[p.name for p in assets]},ensure_ascii=False));return 0
    title=next((l.lstrip('# ').strip() for l in notes.splitlines() if l.startswith('# ')),tag)
    if staged:git('commit','-q','-m',f'{title}\n\n'+'\n'.join(l for l in notes.splitlines()[1:] if l.strip()))
    if not git('tag','-l',tag):git('tag','-a',tag,'-m',title)
    branch=git('rev-parse','--abbrev-ref','HEAD');git('push','-q','-u','origin',branch);git('push','-q','origin',tag)
    if gh('release','view',tag,check=False).returncode:
        gh('release','create',tag,*map(str,assets),'--title',title,'--notes-file',str(notes_path),'--verify-tag')
    url=gh('release','view',tag,'--json','url','-q','.url').stdout.strip()
    print(json.dumps({'tag':tag,'committed':len(staged),'release':url},ensure_ascii=False));return 0

if __name__=='__main__':sys.exit(main())
