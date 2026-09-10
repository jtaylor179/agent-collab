# Update a native Windows Codex install from this checkout.
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$plugin = Join-Path $repo 'plugins\agent-collab'

Write-Host '== agent-collab test suite =='
python -m unittest collab.test_collab -q
if ($LASTEXITCODE -ne 0) { throw 'agent-collab tests failed' }

Write-Host '== version and package consistency =='
python (Join-Path $repo 'check_version.py')
if ($LASTEXITCODE -ne 0) { throw 'agent-collab package is not release-consistent' }

if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    throw 'codex is not available on PATH'
}

codex plugin marketplace add $repo 2>$null
codex plugin add 'agent-collab@agent-collab-marketplace'
if ($LASTEXITCODE -ne 0) { throw 'Codex plugin installation failed' }

# Compatibility for Codex builds that load ~/.codex/skills directly.
$legacy = Join-Path $env:USERPROFILE '.codex\skills\agent-collab'
New-Item -ItemType Directory -Force -Path $legacy | Out-Null
Copy-Item -Recurse -Force (Join-Path $plugin 'skills\agent-collab\*') $legacy

Write-Host "Installed agent-collab from $plugin"
Write-Host 'Restart Codex and use a new task to load the updated skill.'
