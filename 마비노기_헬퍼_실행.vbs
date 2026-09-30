Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
strDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = strDir

strPyw = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Python\pythoncore-3.14-64\pythonw.exe")
If Not fso.FileExists(strPyw) Then
    strPyw = "pythonw.exe"
End If

WshShell.Run """" & strPyw & """ """ & strDir & "\web_server.py""", 0, False
