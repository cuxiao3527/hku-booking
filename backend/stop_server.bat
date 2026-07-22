@echo off
chcp 65001 >nul
title 停止港大预约系统
echo 正在停止港大预约系统...
taskkill /f /im HKUBookingWeb.exe 2>nul
taskkill /f /im 港大预约系统.exe 2>nul
echo 服务已停止
timeout /t 2 >nul
