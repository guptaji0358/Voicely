#pragma once
// Hands the process's idle pages back to Windows (they move to the standby list / page file and are faulted back in only
// if something touches them again). Nothing is lost and nothing changes for the user - the working set simply shrinks
// while the app is minimised or idle. No-op on other platforms.
#include <QtGlobal>
#ifdef Q_OS_WIN
#include <windows.h>
#endif

namespace MemoryTrim {
inline void now() {
#ifdef Q_OS_WIN
    SetProcessWorkingSetSize(GetCurrentProcess(), static_cast<SIZE_T>(-1), static_cast<SIZE_T>(-1));
#endif
}
}  // namespace MemoryTrim
