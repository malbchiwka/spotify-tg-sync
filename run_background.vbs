Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
servicePy = scriptDir & "\service.py"

WshShell.CurrentDirectory = scriptDir
WshShell.Run "pythonw.exe """ & servicePy & """", 0, False
