param([string]$Compiler = '', [switch]$Publish)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $projectRoot
$env:PYTHONIOENCODING = 'utf-8'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
& $python -m pytest -q -p no:cacheprovider
if ($LASTEXITCODE -ne 0) { throw 'Tests failed; release stopped' }
& $python (Join-Path $PSScriptRoot 'release_v19.py') --compiler $Compiler $(if($Publish){'--publish'}else{'--build-only'})
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }

