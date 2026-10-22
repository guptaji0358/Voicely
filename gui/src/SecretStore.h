#pragma once
// Remembers the Google API key between runs without writing it in plain text: on Windows it is encrypted with DPAPI
// (CryptProtectData, bound to the current Windows user) and the encrypted blob is kept in QSettings. Other platforms
// do not persist it. The key is never logged and never shown again except in the masked Settings field.
#include "Config.h"

#include <QByteArray>
#include <QString>

#ifdef Q_OS_WIN
#include <windows.h>
#include <wincrypt.h>
#endif

namespace SecretStore {

inline QByteArray protect(const QByteArray &plain) {
#ifdef Q_OS_WIN
    DATA_BLOB in{DWORD(plain.size()), reinterpret_cast<BYTE *>(const_cast<char *>(plain.constData()))}, out{};
    if (!CryptProtectData(&in, L"Voicely API key", nullptr, nullptr, nullptr, CRYPTPROTECT_UI_FORBIDDEN, &out)) return {};
    QByteArray r(reinterpret_cast<const char *>(out.pbData), int(out.cbData));
    LocalFree(out.pbData);
    return r;
#else
    Q_UNUSED(plain)
    return {};
#endif
}

inline QByteArray unprotect(const QByteArray &blob) {
#ifdef Q_OS_WIN
    DATA_BLOB in{DWORD(blob.size()), reinterpret_cast<BYTE *>(const_cast<char *>(blob.constData()))}, out{};
    if (!CryptUnprotectData(&in, nullptr, nullptr, nullptr, nullptr, CRYPTPROTECT_UI_FORBIDDEN, &out)) return {};
    QByteArray r(reinterpret_cast<const char *>(out.pbData), int(out.cbData));
    SecureZeroMemory(out.pbData, out.cbData);
    LocalFree(out.pbData);
    return r;
#else
    Q_UNUSED(blob)
    return {};
#endif
}

inline QString loadApiKey() {
    const QByteArray blob = QByteArray::fromBase64(Config::settings().value("apiKeyEnc").toByteArray());
    return blob.isEmpty() ? QString() : QString::fromUtf8(unprotect(blob));
}

// An empty key removes the stored copy (only done when the user clears the field and saves).
inline void saveApiKey(const QString &key) {
    if (key.isEmpty()) {
        Config::settings().remove("apiKeyEnc");
        return;
    }
    const QByteArray blob = protect(key.toUtf8());
    if (!blob.isEmpty()) Config::settings().setValue("apiKeyEnc", blob.toBase64());
}

}  // namespace SecretStore
