@echo off
chcp 65001 >nul
title Trosa

rem Trosa is served from the formal ECS PostgreSQL runtime.  This user-facing
rem launcher opens that workspace and never starts a local SQLite writer.
set "TROSA_PUBLIC_URL=https://app.trosa.space"
echo Opening the formal Trosa workspace...
start "" "%TROSA_PUBLIC_URL%"
