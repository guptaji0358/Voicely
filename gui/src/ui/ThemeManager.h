#pragma once
// Loads the Qt stylesheets (assets/styles/*.qss, compiled into the :/styles resource prefix).
//
//   <theme>.qss       colour tokens   "@name: #hex;"
//   components.qss    shared layout tokens + the actual QSS rules, written with "@name" references
//
// The two are merged and the tokens substituted, then applied application-wide. A missing/invalid
// file never leaves the app unstyled: a small built-in fallback style is applied and lastError()
// says why. Set TTS_STYLE_DIR to load the .qss files from a folder instead (live editing, tests).
#include <QColor>
#include <QHash>
#include <QObject>
#include <QStringList>

class ThemeManager : public QObject {
    Q_OBJECT
public:
    static ThemeManager &instance();
    static QStringList availableThemes();            // {"dark"}

    void ensureApplied();                            // applies the theme once (there is only the dark theme)
    bool apply(const QString &theme);                // false => fallback style is in use, see lastError()

    QString current() const { return m_current; }
    QString lastError() const { return m_error; }
    bool usingFallback() const { return m_fallback; }
    QString styleSheet() const { return m_qss; }

    QString token(const QString &name) const { return m_tokens.value(name); }
    QColor color(const QString &name, const QColor &fallback = QColor(0x88, 0x88, 0x88)) const;
    int pixels(const QString &name, int fallback) const;   // "6px" -> 6

signals:
    void changed();

private:
    ThemeManager() = default;
    QHash<QString, QString> m_tokens;
    QString m_current, m_error, m_qss;
    bool m_applied = false, m_fallback = false;
};
