#include "ThemeManager.h"

#include "Config.h"

#include <QApplication>
#include <QDir>
#include <QFile>
#include <QRegularExpression>
#include <QResource>

// Must live at global scope (Q_INIT_RESOURCE declares an extern function). The library is static, so
// without this call the linker may drop the compiled-in resources.
static void initTtsResources() { Q_INIT_RESOURCE(resources); }

namespace {

// Last resort when the .qss files cannot be read. Intentionally tiny.
const char *kFallbackTokens = R"(
@bg: #14161b; @surface: #1a1d24; @surface-raised: #20242c; @border: #2a2f3a; @text: #dde1e9;
@text-muted: #8b93a4; @text-disabled: #5d6475; @accent: #3d7eff; @on-accent: #ffffff;
@success: #3ddc84; @warning: #e5b84a; @danger: #ff6b6b;
@fx-hover: #14ffffff; @fx-press: #30000000; @icon: #c4cad6; @icon-active: #ffffff;
@icon-disabled: #5d6475; @icon-accent: #6ea0ff; @wave: #3d7eff; @wave-idle: #2e3442; @radius: 6px;
)";
const char *kFallbackQss = R"(
QWidget { background: @bg; color: @text; }
QPlainTextEdit, QLineEdit, QComboBox, QDoubleSpinBox { background: @surface-raised; border: 1px solid @border; }
QPushButton { background: @surface-raised; border: 1px solid @border; padding: 4px 10px; }
QPushButton:disabled { color: @text-disabled; }
QPushButton#primary { background: @accent; color: @on-accent; }
)";

QHash<QString, QString> parseTokens(QString &text) {
    static const QRegularExpression def(R"(@([A-Za-z0-9_-]+)[ \t]*:[ \t]*([^;\n]+);)");
    QHash<QString, QString> t;
    auto it = def.globalMatch(text);
    while (it.hasNext()) {
        const auto m = it.next();
        t.insert(m.captured(1), m.captured(2).trimmed());
    }
    text.remove(def);
    return t;
}

QString expand(const QString &text, const QHash<QString, QString> &tokens, QStringList *unresolved) {
    static const QRegularExpression ref(R"(@([A-Za-z0-9_-]+))");
    QString out;
    qsizetype last = 0;
    auto it = ref.globalMatch(text);
    while (it.hasNext()) {
        const auto m = it.next();
        out += text.mid(last, m.capturedStart() - last);
        const QString name = m.captured(1);
        if (tokens.contains(name)) {
            out += tokens.value(name);
        } else {
            if (unresolved && !unresolved->contains(name)) unresolved->append(name);
            out += m.captured(0);
        }
        last = m.capturedEnd();
    }
    return out + text.mid(last);
}

}  // namespace

ThemeManager &ThemeManager::instance() {
    static ThemeManager m;
    return m;
}

QStringList ThemeManager::availableThemes() { return {"dark", "light"}; }

void ThemeManager::ensureApplied() {
    if (!m_applied) apply(Config::settings().value("theme", "dark").toString());
}

bool ThemeManager::apply(const QString &theme) {
    initTtsResources();
    m_applied = true;
    const QString envDir = qEnvironmentVariable("TTS_STYLE_DIR");
    const QString dir = envDir.isEmpty() ? QStringLiteral(":/styles") : envDir;

    QStringList problems;
    auto read = [&](const QString &file) {
        QFile f(dir + "/" + file);
        if (!f.open(QIODevice::ReadOnly | QIODevice::Text)) {
            problems << QString("cannot open %1 (%2)").arg(QDir::toNativeSeparators(f.fileName()), f.errorString());
            return QString();
        }
        const QString s = QString::fromUtf8(f.readAll());
        if (s.trimmed().isEmpty()) problems << f.fileName() + " is empty";
        return s;
    };
    const QString themeText = read(theme + ".qss");
    const QString compText = read("components.qss");

    QHash<QString, QString> tokens;
    QString body;
    if (problems.isEmpty()) {
        QString all = themeText + "\n" + compText;
        tokens = parseTokens(all);
        for (const char *req : {"bg", "text", "accent", "surface", "icon"})
            if (!tokens.contains(req)) problems << QString("theme \"%1\" does not define @%2").arg(theme, req);
        QStringList unresolved;
        body = expand(all, tokens, &unresolved);
        if (!unresolved.isEmpty()) problems << "undefined tokens: @" + unresolved.join(", @");
    }

    m_fallback = !problems.isEmpty();
    if (m_fallback) {
        m_error = QString("Stylesheet \"%1\" failed to load: %2. Using the built-in fallback style.")
                      .arg(theme, problems.join("; "));
        qWarning().noquote() << m_error;
        QString tk = QString::fromLatin1(kFallbackTokens);
        tokens = parseTokens(tk);
        body = expand(QString::fromLatin1(kFallbackQss), tokens, nullptr);
    } else {
        m_error.clear();
    }
    m_tokens = tokens;
    m_qss = body;
    m_current = theme;
    qApp->setStyleSheet(m_qss);
    emit changed();
    return !m_fallback;
}

QColor ThemeManager::color(const QString &name, const QColor &fallback) const {
    const QString v = m_tokens.value(name);
    if (v.isEmpty()) return fallback;
    const QColor c(v);
    return c.isValid() ? c : fallback;
}

int ThemeManager::pixels(const QString &name, int fallback) const {
    const QString v = m_tokens.value(name);
    bool ok = false;
    const int n = v.chopped(v.endsWith("px") ? 2 : 0).toInt(&ok);
    return ok ? n : fallback;
}
