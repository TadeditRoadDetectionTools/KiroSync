@echo off
rem KiroSync 全域啟動器 (Windows): 問上傳分類 -> 背景同步 -> 啟動 Kiro CLI。
rem 把 client 這個資料夾加進 PATH 後, 在任何專案資料夾打 `ks-kiro` 即可。
rem %~dp0 = 這個 .cmd 所在資料夾 (client\), 用它定位 run.py。
python "%~dp0run.py" kiro %*
