param([switch]$BrowserOnly, [switch]$UseHostDns, [switch]$UsePcLocation)
$ErrorActionPreference = 'Stop'
$cabinRoot = $PSScriptRoot
$cabinPython = Join-Path $cabinRoot '.venv\Scripts\python.exe'
$cabinSdk = Join-Path $cabinRoot 'tools\android-sdk'
$cabinAdb = Join-Path $cabinSdk 'platform-tools\adb.exe'
$cabinApk = Join-Path $cabinRoot 'android\build\cabin-conversation.apk'
$env:PYTHONIOENCODING = 'utf-8'
$cabinLocationScript = Join-Path $cabinRoot 'host-location.ps1'
$cabinWindowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$cabinLaunchMutex = New-Object Threading.Mutex($false, 'Local\CabinLauncher_8765')
$cabinOwnsLaunchMutex = $false

function Start-CabinLocationBridge([string]$Mode, [string]$DeviceSerial = '') {
    # PC location is an explicit emulator-development input, never the app default.
    if (-not $UsePcLocation) { return }
    $bridgeArguments = '-NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File "{0}" -Watch -IntervalSeconds 45 -{1}' -f $cabinLocationScript, $Mode
    if ($DeviceSerial) { $bridgeArguments += ' -Serial ' + $DeviceSerial }
    $null = Start-Process -FilePath $cabinWindowsPowerShell -ArgumentList $bridgeArguments `
        -WorkingDirectory $cabinRoot -WindowStyle Hidden
}

function Start-CabinServer([bool]$EnablePcLocation) {
    $previousLocationMode = [Environment]::GetEnvironmentVariable('CABIN_EMULATOR_HOST_LOCATION', 'Process')
    try {
        # Always provide an explicit value. A parent's development environment
        # must not silently enable PC location for a normal launch.
        $env:CABIN_EMULATOR_HOST_LOCATION = if ($EnablePcLocation) { '1' } else { '0' }
        $serverPath = Join-Path $cabinRoot 'server.py'
        return Start-Process -FilePath $cabinPython -ArgumentList @('"' + $serverPath + '"') `
            -WorkingDirectory $cabinRoot -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $cabinRoot 'server.log') `
            -RedirectStandardError (Join-Path $cabinRoot 'server-error.log') -PassThru
    } finally {
        [Environment]::SetEnvironmentVariable('CABIN_EMULATOR_HOST_LOCATION', $previousLocationMode, 'Process')
    }
}

function Assert-CabinLocationMode($Status, [bool]$EnablePcLocation) {
    $serverMode = $Status.development.host_location_enabled
    if ($serverMode -is [bool] -and $serverMode -eq $EnablePcLocation) { return }
    $startCommand = if ($EnablePcLocation) { './start.ps1 -UsePcLocation' } else { './start.ps1' }
    if ($BrowserOnly) { $startCommand += ' -BrowserOnly' }
    throw "실행 중인 서버의 위치 입력 설정이 다르거나 이전 버전이에요. 서버를 종료한 뒤 $startCommand 명령으로 다시 시작해 주세요. 기존 서버와 대화 데이터는 변경하지 않았어요."
}

function Assert-CabinAvd([string]$EmulatorPath, [string]$AvdName = 'Pleos_Connect') {
    if (-not (Test-Path -LiteralPath $EmulatorPath -PathType Leaf)) {
        throw 'Android 에뮬레이터 실행 파일이 없어요. SDK 복구를 먼저 완료해 주세요.'
    }
    $previousErrorPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5.1 wraps native stderr as ErrorRecord even for
        # harmless warnings. Inspect the command result instead of throwing early.
        $ErrorActionPreference = 'Continue'
        $availableAvds = @(& $EmulatorPath -list-avds 2>&1)
        $listExitCode = $LASTEXITCODE
    } catch { throw 'Pleos 가상기기 목록을 확인하지 못했어요. Android 에뮬레이터 설치를 확인해 주세요.' }
    finally { $ErrorActionPreference = $previousErrorPreference }
    $commandErrors = @($availableAvds | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] -and $_.FullyQualifiedErrorId -notmatch '^NativeCommandError' })
    if ($listExitCode -ne 0 -or $commandErrors.Count) { throw 'Pleos 가상기기 목록을 확인하지 못했어요. Android 에뮬레이터 설치를 확인해 주세요.' }
    if (@($availableAvds | ForEach-Object { ([string]$_).Trim() }) -notcontains $AvdName) {
        throw "이 PC에 $AvdName 가상기기가 없어요. SDK 복구만으로 가상기기가 만들어지지는 않아요. Pleos 가상기기를 먼저 생성하거나 ./start.ps1 -BrowserOnly로 웹 화면을 사용해 주세요."
    }
}

function Start-CabinAndroidApp([string]$DeviceSerial, [string]$AndroidUser) {
    $previousErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $launchLines = @(& $cabinAdb -s $DeviceSerial shell am start --user $AndroidUser -n local.pleos.cabin/.MainActivity 2>&1)
        $launchExitCode = $LASTEXITCODE
    } catch { throw '앱 화면을 열지 못했어요. Pleos 에뮬레이터 연결과 앱 설치 상태를 확인해 주세요.' }
    finally { $ErrorActionPreference = $previousErrorPreference }
    $commandErrors = @($launchLines | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] -and $_.FullyQualifiedErrorId -notmatch '^NativeCommandError' })
    $launchOutput = ($launchLines | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
    # am can print an Activity Manager error even when adb itself exits normally.
    if ($launchExitCode -ne 0 -or $commandErrors.Count -or $launchOutput -match '(?im)^\s*(?:Error\b|Exception\b|(?:java\.[\w.]+\.)?(?:SecurityException|ActivityNotFoundException)\b)') {
        throw '앱 화면을 열지 못했어요. Pleos 에뮬레이터 연결과 앱 설치 상태를 확인해 주세요.'
    }
    if ($launchOutput.Trim()) { Write-Output $launchOutput.Trim() }
}

try {
try { $cabinOwnsLaunchMutex = $cabinLaunchMutex.WaitOne(0) }
catch [Threading.AbandonedMutexException] { $cabinOwnsLaunchMutex = $true }
if (-not $cabinOwnsLaunchMutex) {
    Write-Output '이미 앱을 시작하고 있어요. 잠시 기다리면 화면이 열립니다.'
    return
}

if (-not (Test-Path -LiteralPath $cabinPython)) {
    throw '전용 Python 환경이 없어요. README의 설치 단계를 확인해 주세요.'
}
try {
    $cabinStatus = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/state' -TimeoutSec 2
    if (-not $cabinStatus.models) { throw '다른 서버가 8765 포트를 사용하고 있어요.' }
} catch {
    $listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    if ($listener) { throw '8765 포트를 다른 프로그램이 사용하고 있어요. 해당 프로그램을 확인해 주세요.' }
    $null = New-Item -ItemType Directory -Path (Join-Path $cabinRoot 'data') -Force
    $serverProcess = Start-CabinServer ([bool]$UsePcLocation)
    $serverProcess.Id | Set-Content -LiteralPath (Join-Path $cabinRoot 'data\server.pid')
    $connected = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if ($serverProcess.HasExited) { break }
        try { $cabinStatus = Invoke-RestMethod 'http://127.0.0.1:8765/api/state' -TimeoutSec 2; if ($cabinStatus.models) { $connected = $true; break } }
        catch { Start-Sleep -Seconds 1 }
    }
    if (-not $connected) { throw '음성 분석 서버에 연결하지 못했어요. server-error.log를 확인해 주세요.' }
}
Assert-CabinLocationMode $cabinStatus ([bool]$UsePcLocation)
# Only an explicit development launch starts the temporary PC publisher.
if ($UsePcLocation) { Start-CabinLocationBridge 'AppOnly' }
if ($BrowserOnly) {
    Start-Process 'http://127.0.0.1:8765/'
    Write-Output '웹 화면을 열었어요. 음성 준비가 끝나면 대화 시작 버튼이 켜집니다.'
    return
}
if (-not (Test-Path -LiteralPath $cabinAdb) -or -not (Test-Path -LiteralPath $cabinApk)) {
    throw 'Android SDK 또는 APK가 없어요. Android 빌드 단계를 먼저 실행해 주세요.'
}
$cabinDevices = @(& $cabinAdb devices | Select-String '^emulator-\d+\s+device$' | ForEach-Object {($_.Line -split '\s+')[0]})
if ($cabinDevices.Count -eq 0) {
    $cabinImage = Join-Path $cabinSdk 'system-images\android-34\pleos_car_emulator\x86_64'
    if (-not (Test-Path -LiteralPath (Join-Path $cabinImage 'system.img'))) { throw 'Pleos 시스템 이미지 복구가 완료되지 않았어요.' }
    $env:ANDROID_SDK_ROOT = $cabinSdk
    $cabinEmulator = Join-Path $cabinSdk 'emulator\emulator.exe'
    Assert-CabinAvd $cabinEmulator
    # The host driver can reject the WebView EGL configuration on this PC.
    # Use the emulator's supported software renderer for a reproducible preview.
    $cabinEmulatorArgs = '-avd Pleos_Connect -sysdir "{0}" -no-snapshot-load -no-snapshot-save -gpu software' -f $cabinImage
    # The host DNS forwarder can time out on this network. Only the emulator's
    # resolver is changed. Restore host DNS on the next launch with -UseHostDns.
    if (-not $UseHostDns) { $cabinEmulatorArgs += ' -dns-server 8.8.8.8' }
    $cabinEmulatorProcess = Start-Process -FilePath $cabinEmulator -ArgumentList $cabinEmulatorArgs `
        -WindowStyle Normal -RedirectStandardOutput (Join-Path $cabinRoot 'emulator.log') -RedirectStandardError (Join-Path $cabinRoot 'emulator-error.log') -PassThru
    for ($attempt=0; $attempt -lt 150; $attempt++) {
        $cabinDevices = @(& $cabinAdb devices | Select-String '^emulator-\d+\s+device$' | ForEach-Object {($_.Line -split '\s+')[0]})
        if ($cabinDevices.Count -gt 0) { break }
        if ($cabinEmulatorProcess.HasExited) { throw '에뮬레이터가 종료됐어요. emulator.log를 확인해 주세요.' }
        Start-Sleep -Seconds 2
    }
}
if ($cabinDevices.Count -ne 1) { throw '실행할 에뮬레이터가 없거나 여러 대예요. Pleos 하나만 실행한 뒤 다시 시작해 주세요.' }
$cabinSerial = $cabinDevices[0]
for ($attempt=0; $attempt -lt 120; $attempt++) {
    if ((& $cabinAdb -s $cabinSerial shell getprop sys.boot_completed).Trim() -eq '1') { break }
    Start-Sleep -Seconds 2
}
if ((& $cabinAdb -s $cabinSerial shell getprop sys.boot_completed).Trim() -ne '1') { throw '에뮬레이터 부팅이 아직 끝나지 않았어요.' }
$cabinUser = (& $cabinAdb -s $cabinSerial shell am get-current-user | Out-String).Trim()
& $cabinAdb -s $cabinSerial reverse tcp:8765 tcp:8765
& $cabinAdb -s $cabinSerial emu avd hostmicon
# The optional development GPS input is bound to this emulator boot.
if ($UsePcLocation) { Start-CabinLocationBridge 'GpsOnly' $cabinSerial }
& $cabinAdb -s $cabinSerial install --user $cabinUser -r $cabinApk
if ($LASTEXITCODE -ne 0) { throw '앱 설치에 실패했어요.' }
Start-CabinAndroidApp $cabinSerial $cabinUser
Write-Output '모아 앱을 열었어요. 음성 준비가 끝나면 대화 시작 버튼이 켜집니다. 현재 마이크 입력은 좌석 정보가 없어요.'
} finally {
    if ($cabinOwnsLaunchMutex) { $cabinLaunchMutex.ReleaseMutex() }
    $cabinLaunchMutex.Dispose()
}
