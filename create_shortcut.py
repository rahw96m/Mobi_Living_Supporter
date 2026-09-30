import os
import sys
import win32com.client

def create_desktop_shortcut():
    try:
        target_dir = os.path.dirname(os.path.abspath(__file__))
        vbs_path = os.path.join(target_dir, "run_helper.vbs")

        shell = win32com.client.Dispatch("WScript.Shell")
        desktop = shell.SpecialFolders("Desktop")
        shortcut_path = os.path.join(desktop, "마비노기 모바일 헬퍼.lnk")

        shortcut = shell.CreateShortCut(shortcut_path)
        shortcut.TargetPath = "wscript.exe"
        shortcut.Arguments = f'"{vbs_path}"'
        shortcut.WorkingDirectory = target_dir
        shortcut.IconLocation = "shell32.dll,24"
        shortcut.Description = "마비노기 모바일 주간 납품 & 일괄 제작 헬퍼"
        shortcut.Save()

        print(f"SUCCESS: {shortcut_path}")
        return True
    except Exception as e:
        print(f"FAILED: {e}")
        return False

if __name__ == '__main__':
    create_desktop_shortcut()
