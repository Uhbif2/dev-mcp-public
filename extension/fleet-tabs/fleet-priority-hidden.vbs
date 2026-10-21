' Скрытый запуск fleet-priority.ps1 (без мелькания окна). Зовётся из start-fleet-chrome.cmd;
' второй экземпляр держателя сам выходит, так что повторный запуск безвреден.
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & here & "\fleet-priority.ps1""", 0, False
