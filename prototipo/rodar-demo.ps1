# Sobe a API em modo demonstracao e abre o prototipo no navegador.
#
#   .\prototipo\rodar-demo.ps1
#
# Ctrl+C encerra. Os dados sao SIMULADOS -- nenhuma credencial real e usada.

$ErrorActionPreference = "Stop"
$raiz = Split-Path -Parent $PSScriptRoot

$python = Join-Path $raiz ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "Ambiente virtual nao encontrado em .venv -- rode 'python -m venv .venv' primeiro."
}

# Porta ocupada e a armadilha mais traicoeira aqui: o uvicorn novo falha ao
# subir, mas o servidor ANTIGO continua respondendo -- possivelmente em outro
# modo. Voce recebe dados errados e parece defeito na aplicacao.
$ocupada = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($ocupada) {
    $donos = ($ocupada.OwningProcess | Sort-Object -Unique) -join ", "
    Write-Host "A porta 8000 ja esta em uso (PID $donos)." -ForegroundColor Yellow
    Write-Host "Provavelmente e um servidor de um teste anterior." -ForegroundColor Yellow
    $resposta = Read-Host "Encerrar esse processo e continuar? (s/N)"
    if ($resposta -eq "s") {
        $ocupada.OwningProcess | Sort-Object -Unique |
            ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 2
    } else {
        Write-Error "Porta 8000 ocupada. Encerre o processo antes de continuar."
    }
}

$env:DEMO_MODE = "true"
$env:ENV = "development"

# A saida do uvicorn vai para arquivo. Sem isto, uma falha na subida fica
# invisivel (a janela esta escondida) e o script so consegue dizer "nao subiu".
$logSaida = Join-Path $env:TEMP "rastreio-demo-out.log"
$logErro = Join-Path $env:TEMP "rastreio-demo-err.log"

Write-Host "Subindo a API em modo demonstracao (porta 8000)..." -ForegroundColor Cyan

# -WorkingDirectory e OBRIGATORIO: `Start-Process` NAO herda a localizacao do
# PowerShell, usa o diretorio de trabalho do processo. Sem isto o uvicorn sobe
# fora da pasta do projeto, nao encontra `app.main` e morre imediatamente.
$api = Start-Process -FilePath $python `
    -ArgumentList "-m", "uvicorn", "app.main:app", "--port", "8000" `
    -WorkingDirectory $raiz `
    -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $logSaida `
    -RedirectStandardError $logErro

function Mostrar-Falha {
    param([string]$motivo)

    Write-Host ""
    Write-Host $motivo -ForegroundColor Red
    foreach ($arquivo in @($logErro, $logSaida)) {
        if ((Test-Path $arquivo) -and (Get-Item $arquivo).Length -gt 0) {
            Write-Host "--- $arquivo ---" -ForegroundColor DarkGray
            Get-Content $arquivo -Tail 30
        }
    }
    Stop-Process -Id $api.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

# Espera a API responder antes de abrir o navegador: abrir cedo demais mostra
# "API nao encontrada" e faz parecer que algo quebrou.
# Usa 127.0.0.1 em vez de "localhost": em algumas maquinas "localhost" resolve
# primeiro para IPv6 (::1), onde o uvicorn nao esta escutando.
$pronta = $false
foreach ($tentativa in 1..40) {
    if ($api.HasExited) {
        Mostrar-Falha "A API encerrou sozinha durante a subida."
    }
    Start-Sleep -Milliseconds 500
    try {
        Invoke-RestMethod "http://127.0.0.1:8000/health" -TimeoutSec 2 | Out-Null
        $pronta = $true
        break
    } catch { }
}

if (-not $pronta) {
    Mostrar-Falha "A API nao respondeu em 20 segundos."
}

Write-Host "API no ar. Abrindo o prototipo..." -ForegroundColor Green
Start-Process (Join-Path $PSScriptRoot "index.html")

Write-Host ""
Write-Host "Pedidos de teste: 900001 a 900013  |  email: demo@exemplo.com" -ForegroundColor Yellow
Write-Host "Logs em: $logSaida" -ForegroundColor DarkGray
Write-Host "Ctrl+C para encerrar." -ForegroundColor DarkGray

try {
    Wait-Process -Id $api.Id
} finally {
    Stop-Process -Id $api.Id -Force -ErrorAction SilentlyContinue
    Write-Host "API encerrada."
}
