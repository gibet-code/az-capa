[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$azuriteRoot = Join-Path $PSScriptRoot '.azurite-standalone'
$nodePath = (Get-Command node -ErrorAction Stop).Source
$azuriteCli = Get-ChildItem `
    -Path (Join-Path $HOME '.vscode\extensions') `
    -Filter azurite.js `
    -File `
    -Recurse `
    -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match 'azurite\.azurite-[^\\]+\\dist\\src\\azurite\.js$' } |
    Sort-Object FullName -Descending |
    Select-Object -First 1

if (-not $azuriteCli) {
    throw 'Standalone Azurite was not found. Install the Azurite VS Code extension.'
}

function Test-LocalPort {
    param([Parameter(Mandatory)][int] $Port)

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync('127.0.0.1', $Port)
        return $task.Wait(250) -and $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Set-AzuritePriority {
    param(
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)][int[]] $Ports
    )

    $processIds = @(
        $Ports |
        ForEach-Object {
            Get-NetTCPConnection `
                -LocalPort $_ `
                -State Listen `
                -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess
        } |
        Sort-Object -Unique
    )
    if ($processIds.Count -ne 1) {
        throw "Azurite '$Name' ports are owned by $($processIds.Count) processes; expected exactly one."
    }

    $process = Get-Process -Id $processIds[0] -ErrorAction Stop
    $process.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::BelowNormal
    Write-Host "Azurite '$Name' (PID $($process.Id)) priority is $($process.PriorityClass)."
}

function Start-AzuriteInstance {
    param(
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)][int] $BlobPort,
        [Parameter(Mandatory)][int] $QueuePort,
        [Parameter(Mandatory)][int] $TablePort
    )

    $ports = @($BlobPort, $QueuePort, $TablePort)
    $readyPorts = @($ports | Where-Object { Test-LocalPort $_ })
    if ($readyPorts.Count -eq $ports.Count) {
        Write-Host "Azurite '$Name' is already listening."
        Set-AzuritePriority -Name $Name -Ports $ports
        return
    }
    if ($readyPorts.Count -ne 0) {
        throw "Azurite '$Name' has only some expected ports available: $($readyPorts -join ', ')."
    }

    $location = Join-Path $azuriteRoot $Name
    $null = New-Item -ItemType Directory -Path $location -Force
    $arguments = @(
        $azuriteCli.FullName,
        '--silent',
        '--location', $location,
        '--blobHost', '127.0.0.1',
        '--blobPort', "$BlobPort",
        '--queueHost', '127.0.0.1',
        '--queuePort', "$QueuePort",
        '--tableHost', '127.0.0.1',
        '--tablePort', "$TablePort"
    )
    $process = Start-Process `
        -FilePath $nodePath `
        -ArgumentList $arguments `
        -WorkingDirectory $PSScriptRoot `
        -PassThru `
        -WindowStyle Hidden

    $deadline = [DateTime]::UtcNow.AddMinutes(3)
    do {
        if ($process.HasExited) {
            throw "Azurite '$Name' exited with code $($process.ExitCode)."
        }
        if (@($ports | Where-Object { Test-LocalPort $_ }).Count -eq $ports.Count) {
            Write-Host "Started Azurite '$Name' (PID $($process.Id))."
            Set-AzuritePriority -Name $Name -Ports $ports
            return
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "Azurite '$Name' did not become ready within 3 minutes."
}

Start-AzuriteInstance -Name 'app' -BlobPort 11000 -QueuePort 11001 -TablePort 11002
Start-AzuriteInstance -Name 'durable' -BlobPort 12000 -QueuePort 12001 -TablePort 12002
