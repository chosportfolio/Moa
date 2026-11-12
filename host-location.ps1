param(
    [switch]$Watch,
    [switch]$AppOnly,
    [switch]$GpsOnly,
    [ValidateRange(30, 60)][int]$IntervalSeconds = 45,
    [ValidatePattern('^$|^emulator-\d+$')][string]$Serial = ''
)

if ($AppOnly -and $GpsOnly) { throw 'Choose either -AppOnly or -GpsOnly, not both.' }

# WinRT projection is available in Windows PowerShell 5.1, not PowerShell 7.
if ($PSVersionTable.PSVersion.Major -ge 6) {
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $forward = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'RemoteSigned', '-File', $PSCommandPath, '-IntervalSeconds', $IntervalSeconds)
    if ($Watch) { $forward += '-Watch' }
    if ($AppOnly) { $forward += '-AppOnly' }
    if ($GpsOnly) { $forward += '-GpsOnly' }
    if ($Serial) { $forward += @('-Serial', $Serial) }
    & $windowsPowerShell @forward
    exit $LASTEXITCODE
}

$ErrorActionPreference = 'Stop'
$locationAdb = Join-Path $PSScriptRoot 'tools\android-sdk\platform-tools\adb.exe'
$statusPath = Join-Path $PSScriptRoot $(if ($AppOnly) { 'data\host-location-app-status.json' } else { 'data\host-location-status.json' })
$status = [ordered]@{ accuracy_m = $null; source = $null; transport = $(if ($AppOnly) { 'localApp' } else { 'emulatedNmea' }); updated_at = $null; error = $null; app_updated_at = $null; app_error = $null; delivery_status = $null; message = $null }
$locationMutex = $null
$ownsMutex = $false
$expectedBootId = $null
$appServerFailures = 0
$invariant = [Globalization.CultureInfo]::InvariantCulture

function Write-LocationStatus([string]$Failure) {
    $status.error = if ($Failure) { $Failure } else { $null }
    if ($Failure -and -not $AppOnly) {
        $status.delivery_status = 'error'
        $status.message = $null
    }
    $json = $status | ConvertTo-Json -Compress
    # This file contains metadata only. Never put a Geoposition/coordinate here.
    $temporary = $statusPath + '.' + $PID + '.tmp'
    [IO.File]::WriteAllText($temporary, $json, (New-Object Text.UTF8Encoding($false)))
    if ([IO.File]::Exists($statusPath)) { [IO.File]::Replace($temporary, $statusPath, [NullString]::Value) }
    else { [IO.File]::Move($temporary, $statusPath) }
}

function Invoke-LocationAdb([string]$Arguments) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $locationAdb
    $info.Arguments = $Arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        $null = $process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(10000)) {
            $process.Kill()
            throw 'Emulator connection timed out.'
        }
        $output = $stdout.Result
        $null = $stderr.Result
        if ($process.ExitCode -ne 0) { throw 'Emulator connection failed.' }
        return $output
    } finally { $process.Dispose() }
}

function Test-SameEmulator {
    try {
        if ((Invoke-LocationAdb "-s $Serial get-state").Trim() -ne 'device') { return $false }
        $bootId = (Invoke-LocationAdb "-s $Serial shell cat /proc/sys/kernel/random/boot_id").Trim()
        return $bootId -and $bootId -eq $expectedBootId
    } catch { return $false }
}

function Test-AppServer {
    try {
        $reply = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/state' -TimeoutSec 4
        return $null -ne $reply.models
    } catch { return $false }
}

function Read-FreshHostPosition {
    # No RequestAccessAsync: this never opens a consent dialog or changes permissions.
    $operation = $locator.GetGeopositionAsync([TimeSpan]::FromSeconds(10), [TimeSpan]::FromSeconds(10))
    $task = $asTaskMethod.MakeGenericMethod([Windows.Devices.Geolocation.Geoposition]).Invoke($null, @($operation))
    try {
        if (-not $task.Wait(12000)) { throw 'Fresh Windows location was not available in time.' }
        $coordinate = $task.Result.Coordinate
    } catch {
        if ($_.Exception.GetBaseException() -is [UnauthorizedAccessException]) {
            throw 'Windows location access is not allowed for this process.'
        }
        throw 'Windows did not return a fresh location.'
    }
    $age = ([DateTimeOffset]::Now - $coordinate.Timestamp).TotalSeconds
    if ($age -gt 30 -or $age -lt -5) { throw 'Windows returned a stale location; nothing was sent.' }
    $source = $coordinate.PositionSource.ToString()
    if ($source -notin @('WiFi', 'Satellite', 'Cellular')) {
        throw 'Windows returned a default or unverified position source; nothing was sent.'
    }
    if ([double]::IsNaN($coordinate.Accuracy) -or $coordinate.Accuracy -le 0 -or $coordinate.Accuracy -gt 1000) {
        throw 'Windows location accuracy is worse than 1000 metres; nothing was sent.'
    }
    return $coordinate
}

function Convert-NmeaAngle([double]$Value, [int]$DegreeDigits) {
    $absolute = [Math]::Abs($Value)
    $degrees = [Math]::Floor($absolute)
    $minutes = [Math]::Round(($absolute - $degrees) * 60, 7)
    if ($minutes -ge 60) { $degrees++; $minutes = 0 }
    return $degrees.ToString(('0' * $DegreeDigits), $invariant) + $minutes.ToString('00.0000000', $invariant)
}

function New-NmeaSentence([string]$Body) {
    $checksum = 0
    foreach ($value in [Text.Encoding]::ASCII.GetBytes($Body)) { $checksum = $checksum -bxor $value }
    return '$' + $Body + '*' + $checksum.ToString('X2', $invariant)
}

function Send-CheckedNmea($Coordinate) {
    $point = $Coordinate.Point.Position
    $utc = $Coordinate.Timestamp.UtcDateTime
    # Goldfish's public GNSS parser accepts integer HHmmss, not fractional seconds.
    $time = $utc.ToString('HHmmss', $invariant)
    $date = $utc.ToString('ddMMyy', $invariant)
    $latitude = Convert-NmeaAngle $point.Latitude 2
    $longitude = Convert-NmeaAngle $point.Longitude 3
    $northSouth = if ($point.Latitude -lt 0) { 'S' } else { 'N' }
    $eastWest = if ($point.Longitude -lt 0) { 'W' } else { 'E' }
    # Match this emulator's stationary virtual-GPS frame, correcting its checksum.
    # Quality=1, six satellites, altitude/speed/course/variation=0 are SYNTHETIC
    # transport metadata, not satellite/vehicle measurements. Only latitude/longitude
    # and time come from the fresh Windows fix; status retains its true source/accuracy.
    $gga = New-NmeaSentence (@('GPGGA', $time, $latitude, $northSouth, $longitude, $eastWest, '1', '6', '', '0.0', 'M', '0.', 'M', '', '', '') -join ',')
    $rmc = New-NmeaSentence (@('GPRMC', $time, 'A', $latitude, $northSouth, $longitude, $eastWest, '0.00', '0.00', $date, '0.0', 'W') -join ',')
    foreach ($sentence in @($gga, $rmc)) {
        $reply = Invoke-LocationAdb "-s $Serial emu geo nmea $sentence"
        if ($reply -notmatch '(?m)^OK\s*$' -or $reply -match '(?m)^KO') { throw 'The emulator did not accept standard NMEA input.' }
    }
}

function Send-AppHostPosition($Coordinate) {
    # The verified fix goes only to the local recommendation service. Exact
    # coordinates remain in the request/in-memory service state, never status/logs.
    $point = $Coordinate.Point.Position
    $capturedAt = $Coordinate.Timestamp.UtcDateTime.ToString('o', $invariant)
    $body = @{
        latitude = $point.Latitude
        longitude = $point.Longitude
        accuracy_m = $Coordinate.Accuracy
        source = $Coordinate.PositionSource.ToString()
        updated_at = $capturedAt
    } | ConvertTo-Json -Compress
    try {
        $null = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/host-location' -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 4
        $status.app_updated_at = $capturedAt
        $status.app_error = $null
    } catch {
        # Do not copy HTTP bodies or exception details; they may echo coordinates.
        $status.app_error = 'The recommendation app did not accept the current location; the next update will retry.'
    }
}

function Get-GpsSampleTime([string]$Location) {
    # Android Location.toString() exposes the sample's elapsed realtime as et=.
    # Compare this boot-relative clock, not wall time or coordinate equality.
    $token = [regex]::Match($Location, '\bet=(?<time>[^\s\]]+)')
    if (-not $token.Success) { return $null }
    $value = $token.Groups['time'].Value
    if ($value -eq '0') { return [long]0 }
    $duration = [regex]::Match($value, '^\+(?:(?<days>\d+)d)?(?:(?<hours>\d+)h)?(?:(?<minutes>\d+)m)?(?:(?<seconds>\d+)s)?(?:(?<millis>\d+)ms)?$')
    if (-not $duration.Success -or $value -eq '+') { return $null }
    try {
        [long]$total = 0
        foreach ($unit in @(@('days', 86400000), @('hours', 3600000), @('minutes', 60000), @('seconds', 1000), @('millis', 1))) {
            if ($duration.Groups[$unit[0]].Success) {
                $total = [long]($total + [long]$duration.Groups[$unit[0]].Value * $unit[1])
            }
        }
        return $total
    } catch { return $null }
}

function Get-GpsDeliveryBaseline([string]$Dump) {
    $provider = [regex]::Match($Dump, '(?ms)^    gps provider:[ \t]*\r?\n(?<body>.*?)(?=^ {0,4}\S|\z)')
    if (-not $provider.Success) { return $null }
    # Missing/empty/unknown sample time cannot establish a comparison baseline.
    # GPS still receives the fix; a later cycle can verify against that sample.
    [long]$latest = -1
    foreach ($entry in [regex]::Matches($provider.Groups['body'].Value, 'last location=Location\[gps[^\r\n]*')) {
        $sampleTime = Get-GpsSampleTime $entry.Value
        if ($null -eq $sampleTime) { return $null }
        if ($sampleTime -gt $latest) { $latest = $sampleTime }
    }
    if ($latest -lt 0) { return $null }
    return $latest
}

function Get-GpsDeliveryState([string]$Dump, $Point, $AfterElapsedRealtime) {
    # Fused/passive providers also cache Location[gps ...] records. They do not
    # prove that the GPS provider received this fix. Stop at the next peer section.
    $provider = [regex]::Match($Dump, '(?ms)^    gps provider:[ \t]*\r?\n(?<body>.*?)(?=^ {0,4}\S|\z)')
    if (-not $provider.Success) { return 'unverified' }
    $body = $provider.Groups['body'].Value
    # An old coordinate match must never turn an inactive provider into success.
    if ($body -match '(?m)^[ \t]+service: ProviderRequest\[OFF\][ \t]*\r?$') {
        return 'waiting_for_gps_client'
    }
    if ($body -notmatch '(?m)^[ \t]+service: ProviderRequest\[@[^\r\n]*\][ \t]*\r?$') {
        return 'unverified'
    }
    if ($null -eq $AfterElapsedRealtime) { return 'unverified' }
    $locationMatches = [regex]::Matches($body, 'last location=Location\[gps\s+(-?\d+\.\d+),(-?\d+\.\d+)[^\r\n]*')
    foreach ($entry in $locationMatches) {
        $sampleTime = Get-GpsSampleTime $entry.Value
        if ($null -eq $sampleTime -or $sampleTime -le $AfterElapsedRealtime) { continue }
        $actualLatitude = [double]::Parse($entry.Groups[1].Value, $invariant)
        $actualLongitude = [double]::Parse($entry.Groups[2].Value, $invariant)
        if ([Math]::Abs($actualLatitude - $Point.Latitude) -lt 0.00002 -and
            [Math]::Abs($actualLongitude - $Point.Longitude) -lt 0.00002) { return 'verified' }
    }
    return 'unverified'
}

function Send-HostPosition {
    if (-not $AppOnly) { $status.delivery_status = 'checking'; $status.message = $null }
    try { $coordinate = Read-FreshHostPosition }
    catch {
        if (-not $GpsOnly) { $status.app_error = 'Fresh Windows location is unavailable for the recommendation app.' }
        throw
    }
    $point = $coordinate.Point.Position
    if ([double]::IsNaN($point.Latitude) -or [double]::IsNaN($point.Longitude) -or
        $point.Latitude -lt -90 -or $point.Latitude -gt 90 -or
        $point.Longitude -lt -180 -or $point.Longitude -gt 180) {
        if (-not $GpsOnly) { $status.app_error = 'Windows returned invalid coordinates; nothing was sent to the recommendation app.' }
        throw 'Windows returned invalid coordinates; nothing was sent.'
    }
    # App delivery and emulator GPS delivery have independent outcomes. An adb
    # failure must not prevent the recommendation service using this fresh fix.
    if (-not $GpsOnly) { Send-AppHostPosition $coordinate }
    if ($AppOnly) {
        $status.accuracy_m = [Math]::Round($coordinate.Accuracy, 1)
        $status.source = $coordinate.PositionSource.ToString()
        $status.updated_at = $status.app_updated_at
        Write-LocationStatus $null
        return
    }
    if (-not (Test-SameEmulator)) { throw 'The emulator stopped or restarted; location sharing stopped.' }
    # A stationary PC often sends identical coordinates. Record the GPS sample
    # time before sending so an old matching cache cannot prove new delivery.
    $beforeDump = Invoke-LocationAdb "-s $Serial shell dumpsys location"
    $beforeSampleTime = Get-GpsDeliveryBaseline $beforeDump
    # Values stay in memory and the local adb command. No transcript/log/status coordinates.
    $longitude = $point.Longitude.ToString('F7', $invariant)
    $latitude = $point.Latitude.ToString('F7', $invariant)
    $reply = Invoke-LocationAdb "-s $Serial emu geo fix $longitude $latitude"
    if ($reply -notmatch '(?m)^OK\s*$' -or $reply -match '(?m)^KO') { throw 'The emulator did not accept the location.' }
    # Keep Android's emulator fix in sync, and additionally send correctly checksummed
    # standard sentences for the native navigation's strict NMEA parser.
    Send-CheckedNmea $coordinate
    $verified = $false
    for ($attempt = 0; $attempt -lt 5; $attempt++) {
        Start-Sleep -Milliseconds 300
        $dump = Invoke-LocationAdb "-s $Serial shell dumpsys location"
        $delivery = Get-GpsDeliveryState $dump $point $beforeSampleTime
        if ($delivery -eq 'waiting_for_gps_client') {
            $status.delivery_status = $delivery
            $status.message = 'The emulator accepted the position. GPS verification is waiting for an active location client.'
            # updated_at/source/accuracy remain the last verified GPS delivery.
            Write-LocationStatus $null
            return
        }
        $verified = $delivery -eq 'verified'
        if ($verified) { break }
    }
    if (-not $verified) { throw 'The emulator accepted the position but GPS delivery could not be verified.' }
    $status.accuracy_m = [Math]::Round($coordinate.Accuracy, 1)
    $status.source = $coordinate.PositionSource.ToString()
    $status.updated_at = [DateTimeOffset]::Now.ToUniversalTime().ToString('o')
    $status.delivery_status = 'verified'
    Write-LocationStatus $null
}

try {
    if (-not (Test-Path -LiteralPath (Split-Path $statusPath -Parent))) {
        $null = New-Item -ItemType Directory -Path (Split-Path $statusPath -Parent) -Force
    }
    if ($AppOnly) {
        $mutexName = 'Local\CabinHostLocation_App_8765'
    } else {
        if (-not (Test-Path -LiteralPath $locationAdb)) { throw 'Android adb was not found.' }
        if (-not $Serial) {
            $devices = @([regex]::Matches((Invoke-LocationAdb 'devices'), '(?m)^(emulator-\d+)\s+device\s*$') | ForEach-Object { $_.Groups[1].Value })
            if ($devices.Count -ne 1) { throw 'Start exactly one emulator, or specify -Serial.' }
            $Serial = $devices[0]
        }
        $expectedBootId = (Invoke-LocationAdb "-s $Serial shell cat /proc/sys/kernel/random/boot_id").Trim()
        if ($expectedBootId -notmatch '^[a-fA-F0-9-]{36}$' -or -not (Test-SameEmulator)) { throw 'The emulator is not available.' }
        # A replacement boot must not lose its bridge because the old boot's
        # watcher still owns the serial mutex for its final two seconds.
        $mutexName = 'Local\CabinHostLocation_' + $Serial + '_' + $expectedBootId
    }
    $locationMutex = New-Object Threading.Mutex($false, $mutexName)
    try { $ownsMutex = $locationMutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { Write-Output 'The requested location bridge is already running.'; exit 0 }
    if (Test-Path -LiteralPath $statusPath) {
        try {
            $previous = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
            foreach ($key in @('accuracy_m', 'source', 'updated_at')) { $status[$key] = $previous.$key }
            if (-not $GpsOnly) { $status.app_updated_at = $previous.app_updated_at }
        } catch { }
    }
    if (-not $AppOnly -and -not (Test-SameEmulator)) { throw 'The emulator is not available.' }
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Devices.Geolocation.Geolocator, Windows.Devices.Geolocation, ContentType = WindowsRuntime]
    $null = [Windows.Devices.Geolocation.Geoposition, Windows.Devices.Geolocation, ContentType = WindowsRuntime]
    $locator = New-Object Windows.Devices.Geolocation.Geolocator
    $locator.DesiredAccuracy = [Windows.Devices.Geolocation.PositionAccuracy]::High
    $asTaskMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    do {
        if ($AppOnly -and -not (Test-AppServer)) {
            $appServerFailures++
            $status.app_error = if ($appServerFailures -ge 3 -or -not $Watch) { 'The recommendation server is unavailable; start the app again to reconnect location.' } else { 'The recommendation server is unavailable; location updates will retry shortly.' }
            Write-LocationStatus $null
            if (-not $Watch) { Write-Output $status.app_error; exit 1 }
            if (-not $Watch -or $appServerFailures -ge 3) { break }
        } else {
        $appServerFailures = 0
        try {
            Send-HostPosition
            if (-not $Watch) {
                if (-not $AppOnly) {
                    if ($status.delivery_status -eq 'verified') { Write-Output 'Fresh Windows location delivered and verified in emulator GPS. Coordinates were not logged.' }
                    else { Write-Output $status.message }
                }
                if (-not $GpsOnly) {
                    if ($status.app_error) { Write-Output $status.app_error }
                    else { Write-Output 'The recommendation app received the current Windows location.' }
                }
            }
        } catch {
            # All bridge errors above are deliberately coordinate-free.
            $message = $_.Exception.Message
            if ($message -notmatch '^(Windows|Fresh Windows|The emulator|Emulator)') { $message = 'Host location could not be delivered.' }
            Write-LocationStatus $message
            if (-not $Watch) { Write-Output $message; exit 1 }
        }
        }
        if (-not $Watch) { break }
        for ($remaining = $IntervalSeconds; $remaining -gt 0; $remaining -= 2) {
            Start-Sleep -Seconds ([Math]::Min(2, $remaining))
            if (-not $AppOnly -and -not (Test-SameEmulator)) {
                Write-LocationStatus 'The emulator stopped or restarted; location sharing stopped.'
                return
            }
        }
    } while ($true)
} catch {
    $startupFailure = if ($AppOnly) { 'Host location bridge could not start. Check Windows location access.' } else { 'Host location bridge could not start. Check Windows location access and the emulator.' }
    if (-not $GpsOnly) { $status.app_error = $startupFailure }
    Write-LocationStatus $startupFailure
    Write-Output 'Host location bridge could not start. No coordinates were logged.'
    exit 1
} finally {
    if ($ownsMutex) { $locationMutex.ReleaseMutex() }
    if ($locationMutex) { $locationMutex.Dispose() }
}
