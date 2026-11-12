param(
    [string]$SdkRoot = (Join-Path (Split-Path $PSScriptRoot -Parent) 'tools\android-sdk'),
    [string]$JdkRoot = 'C:\Program Files\Android\Android Studio\jbr'
)

$ErrorActionPreference = 'Stop'

function Add-JarResources($Archive, [string[]]$Jars) {
    # D8 converts bytecode, not ClassLoader resources. Kotlin reflection needs
    # its built-ins and ServiceLoader descriptors at their original APK paths.
    $resources = [Collections.Generic.Dictionary[string,byte[]]]::new([StringComparer]::Ordinal)
    foreach ($jarPath in $Jars) {
        $jar = [IO.Compression.ZipFile]::OpenRead($jarPath)
        try {
            foreach ($entry in $jar.Entries) {
                $name = $entry.FullName
                if ($name.EndsWith('/')) { continue }
                if ($name -match '[\\:\x00-\x1f]' -or $name.StartsWith('/') -or
                    $name.Split('/') -contains '..' -or $name.Split('/') -contains '.' -or $name.Split('/') -contains '') {
                    throw "Unsafe JAR resource path in $([IO.Path]::GetFileName($jarPath))."
                }
                if ($name.EndsWith('.class', [StringComparison]::OrdinalIgnoreCase) -or
                    $name -match '(?i)^META-INF/(?:MANIFEST\.MF|[^/]+\.(?:SF|RSA|DSA|EC))$' -or
                    $name -match '(?i)^META-INF/(?:proguard|com\.android\.tools)/') { continue }
                if ($name -match '(?i)^(?:AndroidManifest\.xml|resources\.arsc|classes\d*\.dex)$' -or
                    $name.StartsWith('res/', [StringComparison]::OrdinalIgnoreCase)) {
                    throw "JAR resource collides with an Android build entry: $name"
                }
                if ($entry.Length -gt 16777216) { throw "JAR resource is unexpectedly large: $name" }
                $inputStream = $entry.Open()
                $memory = New-Object IO.MemoryStream
                try { $inputStream.CopyTo($memory); $bytes = $memory.ToArray() }
                finally { $inputStream.Dispose(); $memory.Dispose() }
                if ($resources.ContainsKey($name)) {
                    if ($name.StartsWith('META-INF/services/', [StringComparison]::Ordinal)) {
                        # ServiceLoader accepts multiple providers, one per line.
                        $providers = @([Text.Encoding]::UTF8.GetString($resources[$name]) -split '\r?\n') +
                            @([Text.Encoding]::UTF8.GetString($bytes) -split '\r?\n')
                        $merged = @($providers | ForEach-Object { ($_ -split '#', 2)[0].Trim() } |
                            Where-Object { $_ } | Select-Object -Unique) -join "`n"
                        $resources[$name] = [Text.Encoding]::UTF8.GetBytes($merged + "`n")
                    } elseif ([Convert]::ToBase64String($resources[$name]) -ne [Convert]::ToBase64String($bytes)) {
                        throw "Conflicting JAR resource: $name"
                    }
                } else { $resources.Add($name, $bytes) }
            }
        } finally { $jar.Dispose() }
    }
    foreach ($name in ($resources.Keys | Sort-Object)) {
        if ($null -ne $Archive.GetEntry($name)) { throw "APK resource already exists: $name" }
        $target = $Archive.CreateEntry($name, [IO.Compression.CompressionLevel]::Optimal).Open()
        try { $target.Write($resources[$name], 0, $resources[$name].Length) }
        finally { $target.Dispose() }
    }
    Write-Output ("Packaged {0} JVM resources." -f $resources.Count)
}

$appRoot = $PSScriptRoot
$appBuild = Join-Path $appRoot 'build'
$androidJar = Join-Path $SdkRoot 'platforms\android-34\android.jar'
$toolsRoot = Join-Path $SdkRoot 'build-tools'
$toolVersion = Get-ChildItem -LiteralPath $toolsRoot -Directory |
    Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'aapt2.exe') } |
    Sort-Object { [version]($_.Name -replace '-.*$', '') } -Descending |
    Select-Object -First 1
if (-not $toolVersion) { throw "Android build-tools are missing under $toolsRoot" }
if (-not (Test-Path -LiteralPath $androidJar)) { throw "Android API 34 is missing at $androidJar" }
$appTools = $toolVersion.FullName
$appJava = Join-Path $JdkRoot 'bin\javac.exe'
if (-not (Test-Path -LiteralPath $appJava)) { throw "JDK is missing at $JdkRoot" }
$env:JAVA_HOME = $JdkRoot
Push-Location -LiteralPath $appRoot
try {

New-Item -ItemType Directory -Path $appBuild -Force | Out-Null
$buildRoot = [IO.Path]::GetFullPath($appBuild)
foreach ($childName in @('classes', 'dex')) {
    $child = [IO.Path]::GetFullPath((Join-Path $appBuild $childName))
    if (-not $child.StartsWith($buildRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Build cleanup target is outside the build directory.'
    }
    if (Test-Path -LiteralPath $child) { Remove-Item -LiteralPath $child -Recurse -Force }
    New-Item -ItemType Directory -Path $child -Force | Out-Null
}
$appSources = @(Get-ChildItem -LiteralPath (Join-Path $appRoot 'src') -Recurse -Filter '*.java' |
    ForEach-Object { $_.FullName })
$dependencyJars = @(Get-ChildItem -LiteralPath (Join-Path $appRoot 'deps') -Filter '*.jar' |
    Sort-Object Name | ForEach-Object { $_.FullName })
if ($dependencyJars.Count -lt 10) { throw 'NaviHelper dependencies are missing from android/deps.' }
$compileClasspath = (@($androidJar) + $dependencyJars) -join [IO.Path]::PathSeparator
& $appJava --release 8 -encoding UTF-8 -classpath $compileClasspath -d (Join-Path $appBuild 'classes') @appSources
if ($LASTEXITCODE -ne 0) { throw 'Java compilation failed.' }

Add-Type -AssemblyName System.IO.Compression.FileSystem
$classesJar = Join-Path $appBuild 'cabin-classes.jar'
if (Test-Path -LiteralPath $classesJar) { Remove-Item -LiteralPath $classesJar }
[IO.Compression.ZipFile]::CreateFromDirectory((Join-Path $appBuild 'classes'), $classesJar)
& (Join-Path $appTools 'd8.bat') --min-api 28 --lib $androidJar --output (Join-Path $appBuild 'dex') $classesJar @dependencyJars
if ($LASTEXITCODE -ne 0) { throw 'DEX compilation failed.' }

$resources = Join-Path $appBuild 'resources.zip'
$relativeAndroidJar = (Resolve-Path -LiteralPath $androidJar -Relative)
& (Join-Path $appTools 'aapt2.exe') compile --dir 'res' -o 'build\resources.zip'
if ($LASTEXITCODE -ne 0) { throw 'Resource compilation failed.' }
$unsignedApk = Join-Path $appBuild 'cabin-unsigned.apk'
& (Join-Path $appTools 'aapt2.exe') link -o 'build\cabin-unsigned.apk' -I $relativeAndroidJar `
    --manifest 'AndroidManifest.xml' -A 'assets' --min-sdk-version 28 --target-sdk-version 34 'build\resources.zip'
if ($LASTEXITCODE -ne 0) { throw 'APK resource linking failed.' }
$archive = [IO.Compression.ZipFile]::Open($unsignedApk, [IO.Compression.ZipArchiveMode]::Update)
try {
    foreach ($dexFile in Get-ChildItem -LiteralPath (Join-Path $appBuild 'dex') -Filter '*.dex') {
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $dexFile.FullName, $dexFile.Name) | Out-Null
    }
    Add-JarResources $archive $dependencyJars
    foreach ($required in @('kotlin/kotlin.kotlin_builtins', 'kotlin/collections/collections.kotlin_builtins',
            'META-INF/services/kotlin.reflect.jvm.internal.impl.builtins.BuiltInsLoader',
            'META-INF/services/kotlinx.coroutines.internal.MainDispatcherFactory')) {
        if ($null -eq $archive.GetEntry($required)) { throw "Required JVM runtime resource is missing: $required" }
    }
} finally {
    $archive.Dispose()
}
$alignedApk = Join-Path $appBuild 'cabin-aligned.apk'
& (Join-Path $appTools 'zipalign.exe') -f 4 'build\cabin-unsigned.apk' 'build\cabin-aligned.apk'
if ($LASTEXITCODE -ne 0) { throw 'APK alignment failed.' }
$appKey = Join-Path $appBuild 'cabin-debug.keystore'
if (-not (Test-Path -LiteralPath $appKey)) {
    & (Join-Path $JdkRoot 'bin\keytool.exe') -genkeypair -keystore $appKey `
        -storepass android -keypass android -alias cabin `
        -dname 'CN=Cabin Conversation Local Debug' -keyalg RSA -keysize 2048 -validity 3650
    if ($LASTEXITCODE -ne 0) { throw 'Local debug key creation failed.' }
}
$finalApk = Join-Path $appBuild 'cabin-conversation.apk'
& (Join-Path $appTools 'apksigner.bat') sign --ks $appKey --ks-key-alias cabin `
    --ks-pass pass:android --key-pass pass:android --out $finalApk $alignedApk
if ($LASTEXITCODE -ne 0) { throw 'APK signing failed.' }
& (Join-Path $appTools 'apksigner.bat') verify --verbose $finalApk
if ($LASTEXITCODE -ne 0) { throw 'APK signature verification failed.' }
Write-Output $finalApk
} finally {
    Pop-Location
}
