// Tiny launcher: Voicely.exe -> DLLs\Voicely-app.exe, with DLLs\ on the DLL search path so the Qt/MinGW/ffmpeg
// libraries can live in a subfolder. Forwards the command line, working directory and exit code.
#include <windows.h>
#include <string>

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
    wchar_t self[MAX_PATH];
    DWORD n = GetModuleFileNameW(nullptr, self, MAX_PATH);
    std::wstring dir(self, n);
    dir.erase(dir.find_last_of(L"\\/"));
    const std::wstring libs = dir + L"\\DLLs";
    const std::wstring app = libs + L"\\Voicely-app.exe";

    SetDllDirectoryW(libs.c_str());
    DWORD len = GetEnvironmentVariableW(L"PATH", nullptr, 0);
    std::wstring path(len ? len - 1 : 0, L'\0');
    if (len) GetEnvironmentVariableW(L"PATH", &path[0], len);
    SetEnvironmentVariableW(L"PATH", (libs + (path.empty() ? L"" : L";") + path).c_str());

    // Rebuild the command line: quoted new exe path + everything after our own argv[0].
    const wchar_t* cl = GetCommandLineW();
    if (*cl == L'"') { ++cl; while (*cl && *cl != L'"') ++cl; if (*cl) ++cl; }
    else while (*cl && *cl != L' ' && *cl != L'\t') ++cl;
    std::wstring cmd = L"\"" + app + L"\"" + cl;

    STARTUPINFOW si{}; si.cb = sizeof(si);
    GetStartupInfoW(&si);
    PROCESS_INFORMATION pi{};
    if (!CreateProcessW(app.c_str(), &cmd[0], nullptr, nullptr, FALSE, 0, nullptr, nullptr, &si, &pi)) {
        MessageBoxW(nullptr, (L"Cannot start " + app).c_str(), L"Voicely", MB_ICONERROR);
        return 1;
    }
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 0;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess); CloseHandle(pi.hThread);
    return static_cast<int>(code);
}
