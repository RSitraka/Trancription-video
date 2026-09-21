<#
  Ouvre l'accès à Transcription Vidéo depuis les autres PC du réseau local.

  WSL est en réseau « NAT » : le port du logiciel n'existe que dans WSL, et
  Windows ne le relaie que pour 127.0.0.1. Ce script ajoute donc :
    - une redirection de <IP du serveur>:<port> vers 127.0.0.1:<port> ;
    - une règle de pare-feu entrante pour ce port.
  Les deux survivent au redémarrage. À relancer seulement si l'adresse IP du
  serveur change.

  Utilisation : clic droit sur acces-reseau.cmd → Exécuter en tant qu'administrateur
  Pour tout retirer :  acces-reseau.cmd /retirer
#>
param([int]$Port = 8100, [switch]$Retirer)

$ErrorActionPreference = 'Stop'
$nom = "Transcription Video ($Port)"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
      ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host "Droits administrateur nécessaires : relance avec élévation…" -ForegroundColor Yellow
  $args = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Port $Port"
  if ($Retirer) { $args += " -Retirer" }
  Start-Process powershell.exe -Verb RunAs -ArgumentList $args
  return
}

if ($Retirer) {
  netsh interface portproxy delete v4tov4 listenport=$Port | Out-Null
  Get-NetFirewallRule -DisplayName $nom -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  Write-Host "Accès réseau retiré : le logiciel n'est plus joignable que depuis ce serveur." -ForegroundColor Green
  Read-Host "Entrée pour fermer"; return
}

# Adresse du serveur sur le réseau local (celle de la carte qui sort vers la passerelle).
$ip = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway }
      ).IPv4Address.IPAddress | Select-Object -First 1
if (-not $ip) { throw "Aucune adresse IPv4 avec passerelle : le serveur est-il connecté au réseau ?" }

netsh interface portproxy delete v4tov4 listenaddress=$ip listenport=$Port 2>$null | Out-Null
netsh interface portproxy add v4tov4 listenaddress=$ip listenport=$Port `
      connectaddress=127.0.0.1 connectport=$Port | Out-Null

if (-not (Get-NetFirewallRule -DisplayName $nom -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName $nom -Direction Inbound -Protocol TCP `
      -LocalPort $Port -Action Allow -Profile Any | Out-Null
}

Write-Host ""
Write-Host "Accès réseau ouvert : http://$ip`:$Port" -ForegroundColor Green
Write-Host "Le jeton d'accès est demandé à la première visite (API_TOKEN dans .env)."
Write-Host ""
netsh interface portproxy show v4tov4
Read-Host "Entrée pour fermer"
