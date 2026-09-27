"""Windows shell glue: the taskbar icon and shortcuts that belong to this app.

What actually decides the taskbar button's icon, established by trying each in
turn on Windows 11 with a probe window and a screenshot of the taskbar:
  * Tk's iconbitmap(default=...)          title bar only; taskbar showed pythonw's icon
  * WM_SETICON on the window              no effect on the taskbar
  * shortcuts carrying the AppUserModelID (Desktop and Start Menu)   no effect
  * the WINDOW's own AppUserModel properties, above all
    System.AppUserModel.RelaunchIconResource, set through
    SHGetPropertyStoreForWindow            <- this is the one the taskbar obeys
The relaunch command is what runs if the button is pinned.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import subprocess
import tempfile
from ctypes import POINTER, Structure, byref, c_ubyte, c_void_p, c_wchar_p, cast, wintypes
from pathlib import Path

log = logging.getLogger(__name__)
APP_ID = "Givvy.WhatnotGivvy.App"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def set_process_app_id() -> None:
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def set_window_icons(tk_window, ico: Path | str) -> bool:
    """Give the top-level window its own big and small icon."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.GetParent.restype = wintypes.HWND
        user32.GetParent.argtypes = [wintypes.HWND]
        hwnd = user32.GetParent(tk_window.winfo_id()) or tk_window.winfo_id()      # Tk wraps the toplevel
        IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x80
        ok = False
        for which, size in ((1, 256), (0, 32)):                                   # ICON_BIG (taskbar, alt-tab), ICON_SMALL
            h = user32.LoadImageW(None, str(ico), IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if h:
                user32.SendMessageW(hwnd, WM_SETICON, which, h)
                ok = True
        return ok
    except Exception as e:
        log.warning("could not set the window icons: %s", e)
        return False


HRESULT = ctypes.c_long


class GUID(Structure):
    _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD), ("d4", c_ubyte * 8)]


class PROPERTYKEY(Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


class PROPVARIANT(Structure):
    _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.USHORT), ("r2", wintypes.USHORT), ("r3", wintypes.USHORT),
                ("p", c_void_p), ("p2", c_void_p)]


def _guid(s: str) -> GUID:
    g = GUID()
    ctypes.windll.ole32.CLSIDFromString(c_wchar_p(s), byref(g))
    return g


FMTID = "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"
PID_RELAUNCH_COMMAND, PID_RELAUNCH_ICON, PID_RELAUNCH_NAME, PID_ID = 2, 3, 4, 5


def set_window_app_properties(hwnd: int, app_id: str, icon_resource: str, relaunch_command: str, display_name: str) -> list:
    shell32 = ctypes.windll.shell32
    shell32.SHGetPropertyStoreForWindow.restype = HRESULT
    shell32.SHGetPropertyStoreForWindow.argtypes = [wintypes.HWND, POINTER(GUID), POINTER(c_void_p)]
    iid = _guid("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
    store = c_void_p()
    hr = shell32.SHGetPropertyStoreForWindow(hwnd, byref(iid), byref(store))
    if hr != 0 or not store:
        return [("SHGetPropertyStoreForWindow", hr)]
    vtbl = cast(cast(store, POINTER(c_void_p))[0], POINTER(c_void_p))
    SetValue = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(vtbl[6])
    Commit = ctypes.WINFUNCTYPE(HRESULT, c_void_p)(vtbl[7])
    Release = ctypes.WINFUNCTYPE(wintypes.ULONG, c_void_p)(vtbl[2])
    out, keep = [], []
    # relaunch properties first, the ID last: the shell reads them when the ID lands
    for pid, value in ((PID_RELAUNCH_ICON, icon_resource), (PID_RELAUNCH_COMMAND, relaunch_command),
                       (PID_RELAUNCH_NAME, display_name), (PID_ID, app_id)):
        buf = ctypes.create_unicode_buffer(value); keep.append(buf)
        key = PROPERTYKEY(_guid(FMTID), pid)
        pv = PROPVARIANT(); pv.vt = 31; pv.p = ctypes.addressof(buf)          # VT_LPWSTR
        out.append((pid, SetValue(store, byref(key), byref(pv))))
    out.append(("commit", Commit(store)))
    Release(store)
    return out



def brand_window(tk_window, ico: Path | str) -> bool:
    """Make the taskbar button ours: icon, name, and what a pinned button launches."""
    try:
        user32 = ctypes.windll.user32
        user32.GetParent.restype = c_void_p
        user32.GetParent.argtypes = [c_void_p]
        hwnd = user32.GetParent(tk_window.winfo_id()) or tk_window.winfo_id()      # Tk wraps the toplevel
        set_window_icons(tk_window, ico)
        if getattr(sys, "frozen", False):
            command = f'"{sys.executable}"'
        else:
            command = f'"{Path(sys.executable).with_name("pythonw.exe")}" -m givvy.app'
        res = set_window_app_properties(hwnd, APP_ID, f"{ico},0", command, "Whatnot Givvy")
        ok = all(hr == 0 for _what, hr in res)
        (log.info if ok else log.warning)("taskbar branding: %s", res)
        return ok
    except Exception as e:
        log.warning("could not brand the taskbar button: %s", e)
        return False


_PS = r'''
param([string]$Lnk, [string]$Target, [string]$Arguments, [string]$WorkDir, [string]$Icon, [string]$AppId)
$w = New-Object -ComObject WScript.Shell
$s = $w.CreateShortcut($Lnk)
$s.TargetPath = $Target; $s.Arguments = $Arguments; $s.WorkingDirectory = $WorkDir
$s.IconLocation = "$Icon,0"; $s.Description = "Whatnot Givvy"; $s.Save()
Add-Type @"
using System; using System.Runtime.InteropServices;
public static class LnkAppId {
  [StructLayout(LayoutKind.Sequential)] public struct PROPERTYKEY { public Guid fmtid; public uint pid; }
  [StructLayout(LayoutKind.Sequential)] public struct PROPVARIANT { public ushort vt; public ushort r1, r2, r3; public IntPtr p; public IntPtr p2; }
  [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IPropertyStore {
    int GetCount(out uint c); int GetAt(uint i, out PROPERTYKEY k);
    int GetValue(ref PROPERTYKEY k, out PROPVARIANT v); int SetValue(ref PROPERTYKEY k, ref PROPVARIANT v); int Commit(); }
  [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
  static extern void SHGetPropertyStoreFromParsingName(string path, IntPtr bc, int flags, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);
  public static void Set(string lnk, string appId) {
    Guid iid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"); IPropertyStore store;
    SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, 2, ref iid, out store);          // GPS_READWRITE
    PROPERTYKEY key = new PROPERTYKEY { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };   // System.AppUserModel.ID
    PROPVARIANT pv = new PROPVARIANT { vt = 31, p = Marshal.StringToCoTaskMemUni(appId) };                       // VT_LPWSTR
    try { Marshal.ThrowExceptionForHR(store.SetValue(ref key, ref pv)); Marshal.ThrowExceptionForHR(store.Commit()); }
    finally { Marshal.FreeCoTaskMem(pv.p); Marshal.ReleaseComObject(store); }
  }
  public static string Get(string lnk) {
    Guid iid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"); IPropertyStore store;
    SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, 0, ref iid, out store);
    PROPERTYKEY key = new PROPERTYKEY { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 }; PROPVARIANT pv;
    store.GetValue(ref key, out pv); string s = pv.vt == 31 ? Marshal.PtrToStringUni(pv.p) : ""; Marshal.ReleaseComObject(store); return s; }
}
"@
[LnkAppId]::Set($Lnk, $AppId)
"appid=" + [LnkAppId]::Get($Lnk)
'''


def make_shortcut(lnk: Path | str, target: Path | str, arguments: str = "", workdir: Path | str = "",
                  icon: Path | str = "", app_id: str = APP_ID) -> str:
    """Create a .lnk carrying our AppUserModelID. Returns PowerShell's output."""
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8-sig") as f:
        f.write(_PS)
        script = f.name
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                            "-Lnk", str(lnk), "-Target", str(target), "-Arguments", arguments,
                            "-WorkDir", str(workdir), "-Icon", str(icon or target), "-AppId", app_id],
                           capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW)
        if r.returncode != 0 or "appid=" + app_id not in r.stdout:
            raise RuntimeError(f"shortcut {lnk}: {r.stdout[-200:]} {r.stderr[-400:]}")
        return r.stdout.strip()
    finally:
        Path(script).unlink(missing_ok=True)
