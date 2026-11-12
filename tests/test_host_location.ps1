# Executes selected production functions with synthetic position/HTTP/ADB doubles.
# It never reads Windows location, launches processes, or contacts a real server.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
function Assert-True($Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
function Read-ScriptAst([string]$Name) {
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $projectRoot $Name), [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw ($errors | ForEach-Object Message | Out-String) }
    return $ast
}
$locationAst = Read-ScriptAst 'host-location.ps1'
$launcherAst = Read-ScriptAst 'start.ps1'
Assert-True ($launcherAst.Extent.Text.TrimEnd() -ceq [IO.File]::ReadAllText((Join-Path $projectRoot 'start.ps1'), [Text.Encoding]::UTF8).TrimEnd()) 'Windows PowerShell must decode the Korean launch guidance correctly'
foreach ($name in @('Send-AppHostPosition', 'Get-GpsSampleTime', 'Get-GpsDeliveryBaseline', 'Get-GpsDeliveryState', 'Send-HostPosition', 'Test-AppServer')) {
    $definition = $locationAst.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    Assert-True ($null -ne $definition) ('Missing production function: ' + $name)
    Invoke-Expression $definition.Extent.Text
}
foreach ($name in @('Start-CabinLocationBridge', 'Start-CabinServer', 'Assert-CabinLocationMode', 'Assert-CabinAvd', 'Start-CabinAndroidApp')) {
    $launcherFunction = $launcherAst.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    Assert-True ($null -ne $launcherFunction) ('Missing launch function: ' + $name)
    Invoke-Expression $launcherFunction.Extent.Text
}

$invariant = [Globalization.CultureInfo]::InvariantCulture
$coordinate = [pscustomobject]@{
    Point = [pscustomobject]@{ Position = [pscustomobject]@{ Latitude = 37.1234567; Longitude = 127.7654321 } }
    Accuracy = 145.5; PositionSource = 'WiFi'
    Timestamp = [DateTimeOffset]::Parse('2026-11-10T01:02:03+00:00')
}
$Serial = 'emulator-5554'
$httpCalls = @()
$adbCalls = @()
$savedStatuses = @()
$launched = @()
$processLaunchFailure = $false
$httpFailure = $false
$serverResponse = [pscustomobject]@{ models = [pscustomobject]@{ready = $false} }
$nmeaCount = 0
$gpsDumpCount = 0
function Read-FreshHostPosition { return $coordinate }
function Test-SameEmulator { return $true }
function Start-Sleep { param($Milliseconds, $Seconds) }
function Send-CheckedNmea($Coordinate) { $script:nmeaCount++ }
function Invoke-LocationAdb([string]$Arguments) {
    $script:adbCalls += $Arguments
    if ($Arguments -like '*dumpsys location') {
        $script:gpsDumpCount++
        $sampleTime = if ($script:gpsDumpCount -eq 1) { '+1s0ms' } else { '+2s0ms' }
        return "    gps provider:`n      service: ProviderRequest[@+10s0ms HIGH_ACCURACY]`n        last location=Location[gps 37.1234567,127.7654321 et=$sampleTime]"
    }
    return 'OK'
}
function Invoke-RestMethod {
    param($Uri, $Method, $ContentType, $Body, $TimeoutSec)
    $script:httpCalls += @{uri = $Uri; method = $Method; body = $Body; timeout = $TimeoutSec}
    if ($httpFailure) { throw 'Synthetic HTTP exception with 37.1234567,127.7654321; never log this.' }
    return $serverResponse
}
function Write-LocationStatus([string]$Failure) {
    $status.error = $Failure
    $json = $status | ConvertTo-Json -Compress
    Assert-True ($json -notmatch '37\.1234567|127\.7654321|latitude|longitude') 'Status leaked exact coordinates'
    $script:savedStatuses += $json
}
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, $RedirectStandardOutput, $RedirectStandardError, [switch]$PassThru)
    $script:launched += @{file = $FilePath; arguments = $ArgumentList; style = $WindowStyle; location_mode = $env:CABIN_EMULATOR_HOST_LOCATION}
    if ($processLaunchFailure) { throw 'Synthetic process launch failure.' }
    if ($PassThru) { return [pscustomobject]@{Id=123;HasExited=$false} }
}
function Reset-Fixture {
    $script:status = [ordered]@{accuracy_m = $null; source = $null; updated_at = $null; error = $null; app_updated_at = $null; app_error = $null}
    $script:httpCalls = @(); $script:adbCalls = @(); $script:savedStatuses = @(); $script:nmeaCount = 0; $script:gpsDumpCount = 0
    $script:httpFailure = $false
}

Reset-Fixture
$AppOnly = $true; $GpsOnly = $false
Send-HostPosition
Assert-True ($httpCalls.Count -eq 1 -and $httpCalls[0].uri -eq 'http://127.0.0.1:8765/api/host-location') 'AppOnly must send to the local recommendation service'
Assert-True ($httpCalls[0].timeout -eq 4) 'Location POST must have a bounded timeout'
$payload = $httpCalls[0].body | ConvertFrom-Json
Assert-True ($payload.latitude -eq $coordinate.Point.Position.Latitude -and $payload.source -eq 'WiFi') 'Send the verified fix without geocoding'
Assert-True ($adbCalls.Count -eq 0 -and $nmeaCount -eq 0) 'AppOnly must not require an emulator or ADB'
Assert-True ($status.app_updated_at -eq $coordinate.Timestamp.UtcDateTime.ToString('o', $invariant)) 'Use the measurement UTC timestamp'

$previousUpdate = $status.app_updated_at
$httpFailure = $true
Send-HostPosition
Assert-True ($status.app_updated_at -eq $previousUpdate -and $status.app_error) 'Failed app updates retain the previous timestamp and expose an error'
Assert-True ($adbCalls.Count -eq 0) 'App failure must not try to start GPS'

Reset-Fixture
$AppOnly = $false; $GpsOnly = $true
Send-HostPosition
Assert-True ($httpCalls.Count -eq 0) 'GpsOnly must not duplicate the app publisher'
Assert-True ($adbCalls.Count -eq 3 -and $nmeaCount -eq 1) 'Preserve GPS/NMEA delivery with pre/post sample verification'
Assert-True ($null -eq $status.app_updated_at) 'GpsOnly must not claim an app update'

Reset-Fixture
$AppOnly = $false; $GpsOnly = $false; $httpFailure = $true
Send-HostPosition
Assert-True ($status.app_error -and $nmeaCount -eq 1) 'Legacy combined mode keeps GPS independent of a failed app update'

$httpFailure = $false
Assert-True (Test-AppServer) 'Model loading still counts as a live app server'
$httpFailure = $true
Assert-True (-not (Test-AppServer)) 'Unavailable server is retryable without leaking exception text'

$cabinLocationScript = Join-Path $projectRoot 'host-location.ps1'
$cabinWindowsPowerShell = 'powershell.exe'
$cabinRoot = $projectRoot
$cabinPython = 'synthetic-python.exe'
$UsePcLocation = $false
Start-CabinLocationBridge 'AppOnly'
Start-CabinLocationBridge 'GpsOnly' 'emulator-5554'
Assert-True ($launched.Count -eq 0) 'Normal launches must never start a PC location helper'
$UsePcLocation = $true
Start-CabinLocationBridge 'AppOnly'
Start-CabinLocationBridge 'GpsOnly' 'emulator-5554'
Assert-True ($launched.Count -eq 2 -and @($launched | Where-Object { $_.style -ne 'Hidden' }).Count -eq 0) 'Both helpers launch hidden'
Assert-True ($launched[0].arguments -match '-AppOnly' -and $launched[0].arguments -notmatch '-Serial') 'Browser helper must not depend on a serial'
Assert-True ($launched[1].arguments -match '-GpsOnly.*-Serial emulator-5554') 'GPS helper uses the selected emulator'

# A parent-shell environment must not silently select the development input.
$savedProcessMode = [Environment]::GetEnvironmentVariable('CABIN_EMULATOR_HOST_LOCATION', 'Process')
try {
    $env:CABIN_EMULATOR_HOST_LOCATION = '1'
    $normalServer = Start-CabinServer $false
    Assert-True ($normalServer.Id -eq 123 -and $launched[-1].location_mode -eq '0') 'Normal server starts explicitly with PC input disabled'
    Assert-True ($env:CABIN_EMULATOR_HOST_LOCATION -eq '1') 'Server launch restores the parent process environment'
    $env:CABIN_EMULATOR_HOST_LOCATION = '0'
    $null = Start-CabinServer $true
    Assert-True ($launched[-1].location_mode -eq '1' -and $launched[-1].style -eq 'Hidden') 'Explicit development server receives enabled mode and stays hidden'
    Assert-True ($env:CABIN_EMULATOR_HOST_LOCATION -eq '0') 'Development launch restores the parent process environment'
    $processLaunchFailure = $true
    $launchFailed = $false
    try { $null = Start-CabinServer $true } catch { $launchFailed = $true }
    Assert-True ($launchFailed -and $env:CABIN_EMULATOR_HOST_LOCATION -eq '0') 'Failed launch also restores its environment'
} finally {
    $processLaunchFailure = $false
    [Environment]::SetEnvironmentVariable('CABIN_EMULATOR_HOST_LOCATION', $savedProcessMode, 'Process')
}

# Run only the production watch-loop AST with all side effects still mocked.
Reset-Fixture
$AppOnly = $true; $GpsOnly = $false; $Watch = $true
$IntervalSeconds = 30; $appServerFailures = 0; $httpFailure = $true
$watchLoop = $locationAst.Find({ param($node) $node -is [System.Management.Automation.Language.DoWhileStatementAst] }, $true)
Assert-True ($null -ne $watchLoop) 'Locate the production watcher loop'
Invoke-Expression $watchLoop.Extent.Text
Assert-True ($appServerFailures -eq 3 -and $httpCalls.Count -eq 3) 'Stop only after three consecutive server failures'
Assert-True ($status.app_error -match 'start the app again' -and $adbCalls.Count -eq 0) 'Stopped browser helper reports recovery without touching GPS'

Write-Output 'Host location: browser-only, GPS-only, legacy fallback, hidden launch and bounded server retry tests passed.'

# Only emulate the two native command boundaries. No real AVD is created/listed,
# and no Android activity or operating-system process is started by these tests.
$availableAvdNames = @('Medium_Phone_API_37.0')
$fakeEmulatorExit = 0
$fakeAdbExit = 0
$fakeAdbOutput = ''
$fakeNativeWarning = $false
$emulatorCalls = @()
$androidCalls = @()
function Test-Path {
    param($LiteralPath, $PathType)
    return $LiteralPath -eq 'Invoke-FakeEmulator'
}
function Invoke-FakeEmulator {
    $script:emulatorCalls += ,@($args)
    $global:LASTEXITCODE = $fakeEmulatorExit
    return $availableAvdNames
}
function Invoke-FakeAdb {
    $script:androidCalls += ,@($args)
    $global:LASTEXITCODE = $fakeAdbExit
    if ($fakeNativeWarning) {
        Write-Error -Message 'Warning: Activity was reused.' -ErrorId NativeCommandError
    }
    return $fakeAdbOutput
}
function Assert-Failure([scriptblock]$Action, [string]$Marker = '') {
    $failed = $false
    try { $null = & $Action }
    catch {
        $failed = $true
        if ($Marker) { Assert-True ($_.Exception.Message -match $Marker) 'Failure must explain the recovery option' }
    }
    Assert-True $failed 'Expected the launch preflight to stop instead of reporting success'
}
$BrowserOnly = $false
$beforeModeChecks = $launched.Count
Assert-CabinLocationMode ([pscustomobject]@{development=[pscustomobject]@{host_location_enabled=$false}}) $false
Assert-CabinLocationMode ([pscustomobject]@{development=[pscustomobject]@{host_location_enabled=$true}}) $true
Assert-Failure { Assert-CabinLocationMode ([pscustomobject]@{development=[pscustomobject]@{host_location_enabled=$false}}) $true } 'start.ps1 -UsePcLocation'
Assert-Failure { Assert-CabinLocationMode ([pscustomobject]@{development=[pscustomobject]@{host_location_enabled=$true}}) $false } 'start.ps1'
Assert-Failure { Assert-CabinLocationMode ([pscustomobject]@{models=@{ready=$true}}) $false } 'start.ps1'
Assert-Failure { Assert-CabinLocationMode ([pscustomobject]@{development=[pscustomobject]@{host_location_enabled='false'}}) $false } 'start.ps1'
$BrowserOnly = $true
Assert-Failure { Assert-CabinLocationMode ([pscustomobject]@{development=[pscustomobject]@{host_location_enabled=$true}}) $false } 'start.ps1 -BrowserOnly'
Assert-True ($launched.Count -eq $beforeModeChecks) 'Mode mismatch must not restart or stop an existing server or launch location helpers'
$BrowserOnly = $false
Write-Output 'Location mode: PC input opt-in, isolated server environment and existing-server mismatch guidance tests passed.'
Assert-Failure { Assert-CabinAvd 'missing-emulator.exe' }
Assert-True ($emulatorCalls.Count -eq 0) 'Missing emulator binary must not launch anything'
Assert-Failure { Assert-CabinAvd 'Invoke-FakeEmulator' } 'BrowserOnly'
Assert-True ($emulatorCalls.Count -eq 1 -and $emulatorCalls[0].Count -eq 1 -and $emulatorCalls[0][0] -eq '-list-avds') 'Missing AVD check is list-only'
$availableAvdNames = @('Medium_Phone_API_37.0', 'Pleos_Connect')
Assert-CabinAvd 'Invoke-FakeEmulator'
$fakeEmulatorExit = 1
Assert-Failure { Assert-CabinAvd 'Invoke-FakeEmulator' }

$cabinAdb = 'Invoke-FakeAdb'
$fakeAdbExit = 1; $fakeAdbOutput = 'Activity manager unavailable'
Assert-Failure { Start-CabinAndroidApp 'emulator-5554' '10' }
$fakeAdbExit = 0; $fakeAdbOutput = 'Error type 3: Activity class does not exist.'
Assert-Failure { Start-CabinAndroidApp 'emulator-5554' '10' }
$fakeAdbOutput = 'Warning: Activity not started, intent has been delivered to currently running top-most instance.'
$null = Start-CabinAndroidApp 'emulator-5554' '10'
Assert-True ($androidCalls.Count -eq 3) 'A normal reused activity must still launch successfully'
Assert-True (-not ($androidCalls[2] -contains '-W')) 'Keep the existing non-waiting Activity Manager launch'
$fakeNativeWarning = $true
$null = Start-CabinAndroidApp 'emulator-5554' '10'
Assert-True ($ErrorActionPreference -eq 'Stop') 'Native warning handling must restore the caller preference'
Write-Output 'Launcher: missing AVD/binary, list failure, Activity Manager errors and normal activity reuse tests passed.'
