#include "Icons.h"

#include "ThemeManager.h"

#include <QDir>
#include <QFile>
#include <QHash>
#include <QIconEngine>
#include <QSvgRenderer>

namespace {

// One parsed renderer per (file, colour). SVGs are tiny; this is GUI-thread only.
QSvgRenderer *renderer(const QString &resourcePath, const QColor &color) {
    static QHash<QString, QSvgRenderer *> cache;
    const QString key = resourcePath + "|" + (color.isValid() ? color.name(QColor::HexRgb) : QString());
    auto it = cache.constFind(key);
    if (it != cache.constEnd()) return it.value();
    QSvgRenderer *r = nullptr;
    QFile f(resourcePath);
    if (f.open(QIODevice::ReadOnly)) {
        QByteArray data = f.readAll();
        if (color.isValid()) data.replace("currentColor", color.name(QColor::HexRgb).toLatin1());
        r = new QSvgRenderer(data);
        if (!r->isValid()) {
            delete r;
            r = nullptr;
        }
    }
    cache.insert(key, r);
    return r;
}

class SvgIconEngine : public QIconEngine {
public:
    SvgIconEngine(QString path, QString base) : m_path(std::move(path)), m_base(std::move(base)) {}

    void paint(QPainter *p, const QRect &rect, QIcon::Mode mode, QIcon::State) override {
        Icons::paint(p, m_path, rect, color(mode));
    }
    QPixmap pixmap(const QSize &size, QIcon::Mode mode, QIcon::State state) override {
        QPixmap pm(size);
        pm.fill(Qt::transparent);
        QPainter p(&pm);
        paint(&p, QRect(QPoint(0, 0), size), mode, state);
        return pm;
    }
    QSize actualSize(const QSize &size, QIcon::Mode, QIcon::State) override { return size; }
    QIconEngine *clone() const override { return new SvgIconEngine(*this); }

private:
    QColor color(QIcon::Mode mode) const {
        const auto &t = ThemeManager::instance();
        switch (mode) {
        case QIcon::Disabled: return t.color(m_base == "on-accent" ? "text-disabled" : "icon-disabled");
        case QIcon::Active:
        case QIcon::Selected: return t.color(m_base == "icon" ? "icon-active" : m_base);
        default: return t.color(m_base);
        }
    }
    QString m_path, m_base;
};

}  // namespace

namespace Icons {

QString path(const QString &name) { return ":/icons/" + name + ".svg"; }

QIcon icon(const QString &name, const QString &colorToken) {
    return QIcon(new SvgIconEngine(path(name), colorToken));
}

void paint(QPainter *p, const QString &resourcePath, const QRectF &rect, const QColor &color) {
    QSvgRenderer *r = renderer(resourcePath, color);
    if (!r) return;
    p->save();
    p->setRenderHint(QPainter::Antialiasing);
    r->render(p, rect);
    p->restore();
}

QPixmap pixmap(const QString &resourcePath, const QSize &size, const QColor &color, qreal dpr) {
    QPixmap pm(size * dpr);
    pm.setDevicePixelRatio(dpr);
    pm.fill(Qt::transparent);
    QPainter p(&pm);
    paint(&p, resourcePath, QRectF(QPointF(0, 0), QSizeF(size)), color);
    return pm;
}

QStringList brokenResources() {
    QStringList bad;
    for (const QString &dir : {QStringLiteral(":/icons"), QStringLiteral(":/illustrations")}) {
        const QStringList files = QDir(dir).entryList({"*.svg"}, QDir::Files);
        if (files.isEmpty()) bad << dir + " (no files; resources not compiled in?)";
        for (const QString &f : files)
            if (!renderer(dir + "/" + f, QColor("#808080"))) bad << dir + "/" + f;
    }
    return bad;
}

QStringList requiredIcons() {
    return {"app-logo", "import", "paste", "clear", "voice", "speaker", "generate", "play", "pause", "stop",
            "save-mp3", "export-wav", "settings", "progress", "success", "warning", "error", "cancel",
            "chevron-right", "chevron-down", "combo-arrow"};
}

}  // namespace Icons
