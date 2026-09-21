@echo off
REM Ouvre (ou retire avec /retirer) l'accès réseau à Transcription Vidéo.
REM Clic droit -> Exécuter en tant qu'administrateur.
set OPTIONS=
if /I "%~1"=="/retirer" set OPTIONS=-Retirer
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0acces-reseau.ps1" %OPTIONS%
