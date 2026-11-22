' Creates a proper "Meeting Copilot" shortcut (Start menu + this folder) that Windows can pin to the taskbar.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
progs = sh.SpecialFolders("Programs")
For Each where In Array(progs, appDir)
  Set lnk = sh.CreateShortcut(where & "\Meeting Copilot.lnk")
  lnk.TargetPath = appDir & "\.venv\Scripts\pythonw.exe"
  lnk.Arguments = """" & appDir & "\copilot.py"""
  lnk.WorkingDirectory = appDir
  lnk.IconLocation = appDir & "\copilot.ico,0"
  lnk.Description = "Meeting Copilot"
  lnk.Save
Next
MsgBox "Done! Open Start > All, find Meeting Copilot under M, right-click it > Pin to taskbar.", 64, "Meeting Copilot"
