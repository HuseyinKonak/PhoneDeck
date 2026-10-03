# Builds phonedeck/assets/applister.jar (a dex jar run on the phone via app_process).
# Needs a JDK on PATH and an Android SDK with build-tools and a platform installed.
$ErrorActionPreference = "Stop"

$sdk = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { "$env:LOCALAPPDATA\Android\Sdk" }
$platform = Get-ChildItem "$sdk\platforms" | Sort-Object Name | Select-Object -Last 1
$buildTools = Get-ChildItem "$sdk\build-tools" | Sort-Object Name | Select-Object -Last 1
$androidJar = Join-Path $platform.FullName "android.jar"
$d8 = Join-Path $buildTools.FullName "d8.bat"

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$classes = Join-Path $PSScriptRoot "build"
$output = Join-Path $root "phonedeck\assets\applister.jar"

if (Test-Path $classes) { Remove-Item -Recurse -Force $classes }
New-Item -ItemType Directory -Force $classes | Out-Null

$sources = Get-ChildItem $PSScriptRoot -Filter *.java | Select-Object -ExpandProperty FullName
javac -encoding UTF-8 -source 8 -target 8 -nowarn -Xlint:-options -cp $androidJar -d $classes $sources
if ($LASTEXITCODE -ne 0) { throw "javac failed" }

$classFiles = Get-ChildItem -Recurse $classes -Filter *.class | Select-Object -ExpandProperty FullName
& $d8 --release --min-api 21 --lib $androidJar --output $output $classFiles
if ($LASTEXITCODE -ne 0) { throw "d8 failed" }

Remove-Item -Recurse -Force $classes
Write-Output "Built $output"
