$ErrorActionPreference = 'Stop'
$probeRoot = $PSScriptRoot
$projectRoot = [IO.Path]::GetFullPath((Join-Path $probeRoot '..\..'))
$probeBuild = Join-Path $probeRoot 'build'
$jdkRoot = 'C:\Program Files\Android\Android Studio\jbr'
$env:JAVA_HOME = $jdkRoot
$androidJar = Join-Path $projectRoot 'tools\android-sdk\platforms\android-34\android.jar'
$buildTools = Join-Path $projectRoot 'tools\android-sdk\build-tools\36.0.0'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$jars = @(Get-ChildItem (Join-Path $projectRoot 'android\deps') -Filter '*.jar' | Sort-Object Name | ForEach-Object { $_.FullName })
foreach ($name in @('classes', 'dex', 'assets')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $probeBuild $name) | Out-Null
}
& $python (Join-Path $probeRoot 'prepare.py') asset
if ($LASTEXITCODE -ne 0) { throw 'Asset creation failed.' }
& (Join-Path $jdkRoot 'bin\javac.exe') --release 8 -encoding UTF-8 -classpath ((@($androidJar) + $jars) -join ';') -d (Join-Path $probeBuild 'classes') (Join-Path $probeRoot 'SpeechSdkProbe.java')
if ($LASTEXITCODE -ne 0) { throw 'Java compilation failed.' }
Add-Type -AssemblyName System.IO.Compression.FileSystem
$classesJar = Join-Path $probeBuild 'classes.jar'
if (Test-Path -LiteralPath $classesJar) { Remove-Item -LiteralPath $classesJar }
[IO.Compression.ZipFile]::CreateFromDirectory((Join-Path $probeBuild 'classes'), $classesJar)
& (Join-Path $buildTools 'd8.bat') --min-api 31 --lib $androidJar --output (Join-Path $probeBuild 'dex') $classesJar @jars
if ($LASTEXITCODE -ne 0) { throw 'D8 compilation failed.' }
Push-Location -LiteralPath $probeRoot
try {
& (Join-Path $buildTools 'aapt2.exe') link -o 'build\unsigned.apk' -I '..\..\tools\android-sdk\platforms\android-34\android.jar' --manifest 'AndroidManifest.xml' --min-sdk-version 31 --target-sdk-version 34 -A 'build\assets'
if ($LASTEXITCODE -ne 0) { throw 'Resource linking failed.' }
& $python (Join-Path $probeRoot 'prepare.py') package
if ($LASTEXITCODE -ne 0) { throw 'Packaging failed.' }
& (Join-Path $buildTools 'zipalign.exe') -f 4 'build\unsigned.apk' 'build\aligned.apk'
if ($LASTEXITCODE -ne 0) { throw 'Alignment failed.' }
& (Join-Path $buildTools 'apksigner.bat') sign --ks (Join-Path $projectRoot 'android\build\cabin-debug.keystore') --ks-key-alias cabin --ks-pass pass:android --key-pass pass:android --out (Join-Path $probeBuild 'speech-sdk-probe.apk') (Join-Path $probeBuild 'aligned.apk')
if ($LASTEXITCODE -ne 0) { throw 'Signing failed.' }
Write-Output (Join-Path $probeBuild 'speech-sdk-probe.apk')
} finally { Pop-Location }
