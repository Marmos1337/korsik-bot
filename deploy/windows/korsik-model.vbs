' Starts korsik-model.ps1 without a console window. Arguments from the shortcut are passed on.
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)

args = ""
For Each arg In WScript.Arguments
    args = args & " " & arg
Next

shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & folder & "\korsik-model.ps1""" & args, 0, False
