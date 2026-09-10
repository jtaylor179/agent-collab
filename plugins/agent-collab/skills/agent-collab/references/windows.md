# Windows and PowerShell

Use this reference when agent-collab runs from native Windows rather than WSL.

## Resolve the bundled CLI

Prefer the installed native Codex plugin cache, falling back to the legacy skill copy:

```powershell
$collabBin = Get-ChildItem -File -Recurse `
  "$env:USERPROFILE\.codex\plugins\cache\agent-collab-marketplace\agent-collab", `
  "$env:USERPROFILE\.codex\skills\agent-collab" `
  -Filter collab.py -ErrorAction SilentlyContinue |
  Where-Object FullName -Match 'skills[\\/]agent-collab[\\/]bin[\\/]collab\.py$' |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1 -ExpandProperty FullName
if (-not $collabBin) { throw "agent-collab's bundled collab.py was not found" }
$collabRoot = Join-Path (Get-Location) '.collab'
```

Invoke commands without Bash interpolation:

```powershell
python $collabBin --root $collabRoot doctor --project X
python $collabBin --root $collabRoot status --project X
```

Use `--body` or a temporary body file instead of `echo | ... --body-file -` when the
payload contains quoting or multiple lines.

## Watchers

Codex and Cursor watchers work natively through the cross-platform launcher:

```powershell
$watch = Join-Path (Split-Path $collabBin) 'collab-watch.py'
python $watch codex X C:\path\to\repo
```

`collab-watch.cmd` is an equivalent convenience wrapper. The Copilot and Antigravity
adapters remain POSIX shell adapters; run their `.sh` launchers from WSL or Git Bash.

Set environment values with PowerShell syntax, for example:

```powershell
$env:COLLAB_AGENT = 'codex-1'
$env:COLLAB_ROOT = $collabRoot
$env:COLLAB_WATCH_DETACH = '1'
```
