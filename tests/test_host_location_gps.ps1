# Run production GPS verification/status functions with synthetic fixtures only.
# No Windows location, real ADB, network, process launch or live status file is used.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
function Assert-True($Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $projectRoot 'host-location.ps1'), [ref]$tokens, [ref]$errors)
Assert-True ($errors.Count -eq 0) 'Production script must parse'
foreach ($name in @('Get-GpsSampleTime', 'Get-GpsDeliveryBaseline', 'Get-GpsDeliveryState', 'Send-HostPosition', 'Write-LocationStatus')) {
    $definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    Assert-True ($null -ne $definition) ('Missing production function: ' + $name)
    Invoke-Expression $definition.Extent.Text
}
$invariant = [Globalization.CultureInfo]::InvariantCulture
$coordinate = [pscustomobject]@{
    Point = [pscustomobject]@{ Position = [pscustomobject]@{ Latitude = 37.1234567; Longitude = 127.7654321 } }
    Accuracy = 145.5; PositionSource = 'WiFi'; Timestamp = [DateTimeOffset]::Now
}
$matching = 'last location=Location[gps 37.1234567,127.7654321 hAcc=5.0 et=+2s0ms]'
$oldMatching = $matching.Replace('et=+2s0ms', 'et=+1s0ms')
$different = 'last location=Location[gps 36.1234567,126.7654321 hAcc=5.0 et=+2s0ms]'
function Gps-Dump([string]$Service, [string]$Location) {
    return "    gps provider:`n      service: $Service`n      last locations:`n        $Location`n  Historical Records:`n"
}
$active = Gps-Dump 'ProviderRequest[@+10s0ms HIGH_ACCURACY]' $matching
$oldActive = Gps-Dump 'ProviderRequest[@+10s0ms HIGH_ACCURACY]' $oldMatching
$inactive = Gps-Dump 'ProviderRequest[OFF]' $matching
$wrong = Gps-Dump 'ProviderRequest[@+10s0ms HIGH_ACCURACY]' $different
$outsideMatch = "    fused provider:`n        $matching`n" + $wrong + "    passive provider:`n        $matching`n"
$cases = @(
    @{name='Active GPS matches'; dump=$active; expected='verified'},
    @{name='Inactive GPS never verifies even when cached coordinates match'; dump=$inactive; expected='waiting_for_gps_client'},
    @{name='Fused/passive cache matches cannot verify a mismatched GPS provider'; dump=$outsideMatch; expected='unverified'},
    @{name='No GPS provider'; dump="    fused provider:`n      service: ProviderRequest[OFF]`n        $matching"; expected='unverified'},
    @{name='Unknown GPS request format'; dump=(Gps-Dump 'unknown' $matching); expected='unverified'},
    @{name='Missing GPS service line'; dump="    gps provider:`n        $matching"; expected='unverified'},
    @{name='Active GPS without a fix'; dump=(Gps-Dump 'ProviderRequest[@+10s0ms]' 'last location=null'); expected='unverified'},
    @{name='Windows line endings'; dump=$active.Replace("`n", "`r`n"); expected='verified'},
    @{name='Active GPS with unchanged matching cached sample'; dump=$oldActive; expected='unverified'},
    @{name='Active GPS with older matching sample'; dump=$oldActive.Replace('et=+1s0ms', 'et=+500ms'); expected='unverified'},
    @{name='Active GPS with missing sample time'; dump=$active.Replace(' et=+2s0ms', ''); expected='unverified'},
    @{name='Active GPS with unknown sample time'; dump=$active.Replace('et=+2s0ms', 'et=unknown'); expected='unverified'}
)
foreach ($case in $cases) {
    Assert-True ((Get-GpsDeliveryState $case.dump $coordinate.Point.Position 1000) -eq $case.expected) $case.name
}
Assert-True ((Get-GpsSampleTime 'Location[gps synthetic et=+1d2h3m4s5ms]') -eq 93784005) 'Elapsed time supports days through milliseconds'
Assert-True ((Get-GpsSampleTime 'Location[gps synthetic et=0]') -eq 0) 'Elapsed zero is a known sample time'
Assert-True ($null -eq (Get-GpsSampleTime 'Location[gps synthetic et=+]')) 'Empty elapsed duration is unknown'
Assert-True ((Get-GpsDeliveryBaseline $oldActive) -eq 1000) 'Capture the GPS sample time before delivery'
Assert-True ((Get-GpsDeliveryBaseline $outsideMatch) -eq 2000) 'Baseline ignores other provider sections'
$emptyGps = Gps-Dump 'ProviderRequest[@+10s0ms]' 'last location=null'
Assert-True ($null -eq (Get-GpsDeliveryBaseline $emptyGps)) 'GPS without a cached fix has no comparison baseline'
Assert-True ((Get-GpsDeliveryState $active $coordinate.Point.Position (Get-GpsDeliveryBaseline $emptyGps)) -eq 'unverified') 'The first sample alone cannot prove progression from an empty baseline'
Assert-True ((Get-GpsDeliveryState $active.Replace('et=+2s0ms', 'et=+3s0ms') $coordinate.Point.Position (Get-GpsDeliveryBaseline $active)) -eq 'verified') 'A later cycle can verify against the established first sample'
Assert-True ($null -eq (Get-GpsDeliveryBaseline "    gps provider:`n      service: ProviderRequest[@+10s0ms]`n")) 'An empty provider section cannot establish a baseline'
Assert-True ($null -eq (Get-GpsDeliveryBaseline 'unknown dump')) 'Missing GPS provider gives no baseline'
Assert-True ($null -eq (Get-GpsDeliveryBaseline $oldActive.Replace(' et=+1s0ms', ''))) 'An existing untimestamped cache gives no baseline'
Assert-True ((Get-GpsDeliveryState $active $coordinate.Point.Position $null) -eq 'unverified') 'Unknown pre-delivery sample state cannot prove delivery'

$AppOnly = $false; $GpsOnly = $true; $Serial = 'emulator-5554'
$script:dumpCount = 0; $script:nmeaCount = 0; $script:currentDump = $inactive; $script:beforeDumpFixture = $oldActive
function Read-FreshHostPosition { return $coordinate }
function Test-SameEmulator { return $true }
function Start-Sleep { param($Milliseconds, $Seconds) }
function Send-CheckedNmea($Coordinate) { $script:nmeaCount++ }
function Invoke-LocationAdb([string]$Arguments) {
    if ($Arguments -like '*dumpsys location') {
        $script:dumpCount++
        if ($script:dumpCount -eq 1) { return $script:beforeDumpFixture }
        return $script:currentDump
    }
    return 'OK'
}
$oldVerifiedAt = '2026-11-01T00:00:00.0000000Z'
function Reset-Status {
    $script:status = [ordered]@{accuracy_m=150; source='WiFi'; transport='emulatedNmea';
        updated_at=$oldVerifiedAt; error='Old failure'; app_updated_at=$null; app_error=$null;
        delivery_status='error'; message=$null}
    $script:dumpCount = 0
}
function Read-SafeTestStatus {
    $json = Get-Content -LiteralPath $statusPath -Raw
    Assert-True ($json -notmatch '37\.1234567|127\.7654321|36\.1234567|126\.7654321|latitude|longitude') 'Status must not contain coordinates'
    return $json | ConvertFrom-Json
}
# Only coordinate-free status metadata is written to a unique disposable file.
$statusPath = Join-Path $PSScriptRoot ('cabin-gps-status-test-' + [guid]::NewGuid().ToString('N') + '.json')
try {
    Reset-Status
    Send-HostPosition
    $saved = Read-SafeTestStatus
    Assert-True ($saved.delivery_status -eq 'waiting_for_gps_client' -and $null -eq $saved.error) 'Inactive GPS is waiting, not an error or verified'
    Assert-True ($saved.updated_at -eq $oldVerifiedAt -and $saved.accuracy_m -eq 150) 'Waiting must retain the last verified measurement metadata'
    Assert-True ($saved.message -match 'waiting for an active location client' -and $dumpCount -eq 2) 'Inactive GPS uses one baseline read and one waiting-state read'

    $script:currentDump = $active
    $script:dumpCount = 0
    Send-HostPosition
    $saved = Read-SafeTestStatus
    Assert-True ($saved.delivery_status -eq 'verified' -and $null -eq $saved.error -and $null -eq $saved.message) 'A later active client clears the waiting message'
    Assert-True ($saved.updated_at -ne $oldVerifiedAt -and $saved.accuracy_m -eq 145.5) 'Only verified GPS advances its measurement metadata'

    foreach ($dump in @($outsideMatch, 'unknown dump', $oldActive)) {
        Reset-Status
        $script:currentDump = $dump
        $caught = $null
        try { Send-HostPosition } catch { $caught = $_.Exception.Message }
        Assert-True ($caught -eq 'The emulator accepted the position but GPS delivery could not be verified.') 'Active mismatch, stale sample and unknown state must remain unverified'
        Assert-True ($dumpCount -eq 6) 'Unverified GPS uses one baseline read and five bounded retries'
        # The watch-loop error path calls this same production writer.
        Write-LocationStatus $caught
        $saved = Read-SafeTestStatus
        Assert-True ($saved.delivery_status -eq 'error' -and $saved.error -eq $caught) 'Failures must not retain a prior waiting or verified state'
        Assert-True ($saved.updated_at -eq $oldVerifiedAt) 'Failure must not advance the verification timestamp'
    }

    Reset-Status
    $script:currentDump = $inactive
    $Watch = $false; $appServerFailures = 0
    $watchLoop = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.DoWhileStatementAst] }, $true)
    $output = @(Invoke-Expression $watchLoop.Extent.Text) -join ' '
    Assert-True ($output -match 'waiting for an active location client') 'One-shot execution must explain the waiting state'
    Assert-True ($output -notmatch 'delivered and verified') 'One-shot execution must not print a false GPS success'
    $null = Read-SafeTestStatus
} finally {
    if (Test-Path -LiteralPath $statusPath) { Remove-Item -LiteralPath $statusPath -Force }
    $temporary = $statusPath + '.' + $PID + '.tmp'
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
}
Write-Output 'GPS delivery: 12 parser cases, sample-time baselines and waiting/recovery/stale-cache/failure/one-shot metadata regressions passed.'
