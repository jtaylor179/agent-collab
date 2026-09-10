# Roster wiring for the agent-collab scale test.
#
#   .\fleet.ps1 -Verify                    # check every agent's CLI + model id first
#   .\fleet.ps1 -Launch -Project scale1    # start one detached watcher per agent
#   .\fleet.ps1 -Stop                      # stop them
#
# One identity per ROLE, not per tool. The bus has no agent-id whitelist, so several
# identities can ride the same CLI with different models (verified: two Cursor ids and
# a Claude worker separate from the architect all claimed distinct tasks). Note this
# bypasses collab-watch.py, whose ALIASES table hardcodes one id + one model per tool.
[CmdletBinding()]
param(
  [switch]$Verify,
  [switch]$Launch,
  [switch]$Stop,
  [string]$Project = "scale1",
  [ValidateSet("all", "cheap", "smart", "reviewers")] [string]$Tier = "all"
)

$ErrorActionPreference = 'Stop'
$SkillBin = Join-Path $PSScriptRoot "..\plugins\agent-collab\skills\agent-collab\bin"
$SkillBin = (Resolve-Path $SkillBin).Path
$Collab   = Join-Path $SkillBin "collab.py"
$CursorEx = Join-Path $SkillBin "cursor-exec.py"
$LogDir   = Join-Path $PSScriptRoot "logs"

# tool  = which adapter drives it
# model = the id handed to that tool
# tier  = cheap | smart | reviewer | architect
$Roster = @(
  [pscustomobject]@{ Id="claude-opus";     Tool="claude"; Model="claude-opus-5";            Tier="architect" }

  [pscustomobject]@{ Id="copilot-luna";    Tool="copilot"; Model="gpt-5.6-luna";            Tier="cheap" }
  [pscustomobject]@{ Id="codex-luna";      Tool="codex";   Model="gpt-5.6-luna";            Tier="cheap" }
  [pscustomobject]@{ Id="cursor-composer"; Tool="cursor";  Model="composer-2.5";            Tier="cheap" }
  [pscustomobject]@{ Id="claude-haiku";    Tool="claude";  Model="claude-haiku-4-5-20251001"; Tier="cheap" }

  [pscustomobject]@{ Id="codex-terra";     Tool="codex";   Model="gpt-5.6-terra";           Tier="smart" }
  [pscustomobject]@{ Id="claude-sonnet";   Tool="claude";  Model="claude-sonnet-5";         Tier="smart" }
  [pscustomobject]@{ Id="cursor-grok";     Tool="cursor";  Model="grok 4.6";                Tier="smart" }
  [pscustomobject]@{ Id="gemini-flash";    Tool="cursor";  Model="gemini-3.8-flash-high";   Tier="smart" }

  [pscustomobject]@{ Id="copilot-terra";   Tool="copilot"; Model="gpt-5.6-terra";           Tier="reviewer" }
)

function Select-Tier {
  switch ($Tier) {
    "cheap"     { $Roster | Where-Object Tier -eq "cheap" }
    "smart"     { $Roster | Where-Object Tier -eq "smart" }
    "reviewers" { $Roster | Where-Object { $_.Tier -in @("reviewer", "architect") } }
    default     { $Roster }
  }
}

# Per-agent environment. This is the whole trick: same CLI, different model, different id.
function Set-AgentEnv($a) {
  $env:CURSOR_MODEL = $null; $env:CLAUDE_MODEL = $null
  $env:COPILOT_MODEL = $null; $env:COLLAB_CODEX_EXEC_ARGS = $null
  switch ($a.Tool) {
    "cursor"  { $env:CURSOR_MODEL = $a.Model }
    "claude"  { $env:CLAUDE_MODEL = $a.Model }
    "copilot" { $env:COPILOT_MODEL = $a.Model }
    "codex"   { $env:COLLAB_CODEX_EXEC_ARGS = (ConvertTo-Json @("-c", "model=$($a.Model)") -Compress) }
  }
  $env:COLLAB_AGENT = $a.Id
}

function Get-ExecArgv($a) {
  switch ($a.Tool) {
    "cursor"  { @("python", $CursorEx) }
    "claude"  { @("claude", "--print", "--permission-mode", "dontAsk",
                  "--no-chrome", "--no-session-persistence", "--model", $a.Model) }
    "codex"   { @("codex", "exec", "-c", "model=$($a.Model)") }
    "copilot" { @("bash", (Join-Path $SkillBin "copilot-exec.sh")) }
  }
}

if ($Verify) {
  Write-Output ("{0,-17} {1,-8} {2,-28} {3}" -f "AGENT", "TOOL", "MODEL", "STATUS")
  Write-Output ("-" * 78)
  foreach ($a in Select-Tier) {
    $status = "?"
    switch ($a.Tool) {
      "cursor" {
        if (-not (Get-Command agent -ErrorAction SilentlyContinue)) { $status = "FAIL: cursor CLI absent" }
        else {
          # Cheapest real check: the CLI rejects unknown ids with a non-zero exit.
          $env:CURSOR_MODEL = $a.Model
          $r = ("probe" | python $CursorEx 2>&1) -join ' '
          $status = if ($LASTEXITCODE -eq 0) { "OK (verified live)" }
                    else { "FAIL: " + $r.Substring(0, [Math]::Min(46, $r.Length)) }
          $env:CURSOR_MODEL = $null
        }
      }
      "claude" {
        if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { $status = "FAIL: claude CLI absent" }
        else {
          # Presence is not enough: a spawned `claude` inherits no login from the host
          # session, and an unauthenticated CLI answers on STDOUT with exit 1 -- which
          # reads as a bad model unless you check here.
          $auth = (& claude auth status 2>&1) -join ' '
          $status = if ($auth -match '"loggedIn"\s*:\s*true') { "OK (authenticated)" }
                    else { "FAIL: not logged in -- run 'claude auth login' in THIS context" }
        }
      }
      "codex" {
        if (-not (Get-Command codex -ErrorAction SilentlyContinue)) { $status = "FAIL: codex CLI absent" }
        else {
          $probe = ("say OK" | & codex exec -m $a.Model - 2>&1) -join ' '
          $status = if ($probe -match 'requires a newer version') { "FAIL: model needs a newer Codex CLI" }
                    elseif ($probe -match '"type":"error"') { "FAIL: model rejected by API" }
                    else { "OK (verified live)" }
        }
      }
      "copilot" {
        $status = "NEEDS Git Bash/WSL on Windows; model id UNVERIFIED"
      }
    }
    Write-Output ("{0,-17} {1,-8} {2,-28} {3}" -f $a.Id, $a.Tool, $a.Model, $status)
  }
  Write-Output ""
  Write-Output "Cursor and Codex rows are probed live; Claude rows check auth. A blocked row"
  Write-Output "is usually a toolchain problem, not a bad model -- Codex rejected gpt-5.6-*"
  Write-Output "until its CLI was upgraded. Cursor also exposes gpt-5.6-luna-*, gpt-5.6-terra-*,"
  Write-Output "gemini-3.8-flash-* and claude-*, so you can re-point a stuck identity there."
  return
}

if ($Launch) {
  if (-not $env:COLLAB_ROOT) { throw "Set COLLAB_ROOT to one shared local-disk path first." }
  New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
  foreach ($a in Select-Tier) {
    if ($a.Tier -eq "architect") { continue }   # the architect drives, it does not claim
    Set-AgentEnv $a
    $log = Join-Path $LogDir "$($a.Id).log"
    $argv = Get-ExecArgv $a
    python $Collab watch --project $Project --agent $a.Id --detach --log $log --exec @argv
    Write-Output ("launched {0,-17} {1,-8} {2}" -f $a.Id, $a.Tool, $a.Model)
  }
  Write-Output ""
  Write-Output "Watchers are detached. Tail a log:  Get-Content $LogDir\<agent>.log -Wait"
  return
}

if ($Stop) {
  Get-Process python -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'collab\.py.*watch' } |
    ForEach-Object { Write-Output "stopping PID $($_.Id)"; Stop-Process -Id $_.Id -Force }
  return
}

Select-Tier | Format-Table Id, Tool, Model, Tier -AutoSize
