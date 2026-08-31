#Requires -Version 5.1
<#
.SYNOPSIS
    Manually trigger the GitHub Actions subscription update workflow and wait for the result.

.DESCRIPTION
    Wraps the equivalent gh CLI sequence used for a manual update:
      1. gh workflow run <workflow> --repo <repo> --ref <ref>   (trigger)
      2. gh run watch <run-id> --exit-status                    (wait, fail loudly if the run fails)
      3. gh run view <run-id>                                   (print the run summary)

    Defaults target this repository's update workflow (update.yml @ main).
    Requires the gh CLI to be installed and authenticated.

.EXAMPLE
    .\run-update.ps1

.EXAMPLE
    .\run-update.ps1 -NoWait          # trigger only, print the run URL and exit

.EXAMPLE
    .\run-update.ps1 -Repo myorg/myrepo -Workflow deploy.yml -Ref dev

.PARAMETER Repo
    GitHub repository in "owner/name" form. Default: muddyneil/proxies.

.PARAMETER Workflow
    Workflow file name (or ID) to trigger. Default: update.yml.

.PARAMETER Ref
    Branch/ref the workflow runs against. Default: main.

.PARAMETER NoWait
    Trigger the run and exit immediately instead of watching it.

.PARAMETER SkipView
    Skip the gh run view summary after the run finishes.

.PARAMETER Interval
    Poll interval in seconds for gh run watch. Default: 15.
#>
[CmdletBinding()]
param(
    [string]$Repo = "muddyneil/proxies",
    [string]$Workflow = "update.yml",
    [string]$Ref = "main",
    [switch]$NoWait,
    [switch]$SkipView,
    [int]$Interval = 15
)

# Intentionally NOT setting $ErrorActionPreference = "Stop": with it, native stderr
# captured via 2>&1 becomes a terminating error under Windows PowerShell 5.1,
# which would abort before our explicit $LASTEXITCODE handling below.

function Assert-GhAvailable {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw "gh CLI not found. Install it first: https://cli.github.com/"
    }
}

# Extract the run id from gh workflow run output (the run URL), with a fallback
# to the newest run of this workflow if the URL is not printed (older gh versions).
function Get-TriggeredRunId {
    param(
        [string]$TriggerOutput,
        [string]$Repo,
        [string]$Workflow
    )

    $match = [regex]::Match($TriggerOutput, 'actions/runs/(\d+)')
    if ($match.Success) {
        return $match.Groups[1].Value
    }

    $id = gh run list --workflow $Workflow --repo $Repo --limit 1 --json databaseId --jq '.[0].databaseId' 2>$null
    if ($LASTEXITCODE -eq 0 -and $id -match '^\d+$') {
        return $id
    }

    throw "Could not determine the triggered run id from: $TriggerOutput"
}

Assert-GhAvailable

Write-Host "Triggering workflow '$Workflow' on $Repo (ref: $Ref) ..." -ForegroundColor Cyan
$output = gh workflow run "$Workflow" --repo $Repo --ref $Ref 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to trigger the workflow:`n$output"
    exit 1
}

$runId = Get-TriggeredRunId -TriggerOutput ($output -join "`n") -Repo $Repo -Workflow $Workflow
$runUrl = "https://github.com/$Repo/actions/runs/$runId"
Write-Host "Run started: $runUrl" -ForegroundColor Green

if ($NoWait) {
    Write-Host "NoWait specified - not watching. Check progress at: $runUrl"
    exit 0
}

Write-Host "Watching run $runId ... (press Ctrl+C to detach)" -ForegroundColor Cyan
gh run watch "$runId" --repo $Repo --interval $Interval --exit-status
$watchExit = $LASTEXITCODE

if (-not $SkipView) {
    Write-Host "`n--- Run summary ---"
    gh run view "$runId" --repo $Repo
}

if ($watchExit -ne 0) {
    Write-Error "Workflow run $runId FAILED (gh exit code: $watchExit). See: $runUrl"
    exit $watchExit
}

Write-Host "Workflow run $runId completed successfully." -ForegroundColor Green
exit 0