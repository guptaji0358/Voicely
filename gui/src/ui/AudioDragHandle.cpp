#include "AudioDragHandle.h"

#include "ThemeManager.h"

#include <QApplication>
#include <QClipboard>
#include <QDrag>
#include <QEnterEvent>
#include <QFileInfo>
#include <QFontMetrics>
#include <QLinearGradient>
#include <QMimeData>
#include <QMouseEvent>
#include <QPainter>
#include <QPixmap>
#include <QUrl>

AudioDragHandle::AudioDragHandle(QWidget *parent) : QWidget(parent) {
    setObjectName("audioDragHandle");
    setMouseTracking(true);
    setFocusPolicy(Qt::NoFocus);
    setAccessibleName("Drag audio into another application");
    setCursor(Qt::ForbiddenCursor);
    setToolTip("No audio yet");
}

void AudioDragHandle::setState(State s) {
    if (s == m_state) return;
    m_state = s;
    setCursor(s == State::Ready ? Qt::OpenHandCursor : s == State::Preparing ? Qt::BusyCursor : Qt::ForbiddenCursor);
    setToolTip(s == State::Ready
                   ? "Drag this into your video editor or a folder - no save dialog.\nClick to copy the file instead.\n"
                     "It is a temporary file; old ones are removed automatically."
               : s == State::Preparing ? "Preparing the file…" : "Generate speech first");
    update();
}

void AudioDragHandle::setFormat(const QString &fmt) {
    if (fmt == m_fmt) return;
    m_fmt = fmt;
    update();
}

QRectF AudioDragHandle::chipRect() const {
    const QRectF r = QRectF(rect()).adjusted(1, 1, -1, -1);
    return QRectF(r.right() - 40, r.top() + 3, 37, r.height() - 6);
}

void AudioDragHandle::enterEvent(QEnterEvent *e) {
    QWidget::enterEvent(e);
    m_hover = true;
    update();
}

void AudioDragHandle::leaveEvent(QEvent *e) {
    QWidget::leaveEvent(e);
    m_hover = false;
    update();
}

void AudioDragHandle::paintEvent(QPaintEvent *) {
    QPainter p(this);
    p.setRenderHint(QPainter::Antialiasing);
    const auto &t = ThemeManager::instance();
    const QColor accent = t.color("accent", QColor("#4d8dff"));
    const bool ready = m_state == State::Ready;
    const QRectF r = QRectF(rect()).adjusted(1, 1, -1, -1);
    const qreal rad = r.height() / 2;

    QLinearGradient g(r.topLeft(), r.bottomLeft());
    QColor a = ready ? accent.darker(m_hover ? 120 : 150) : QColor(255, 255, 255, 14);
    QColor b = ready ? accent.darker(m_hover ? 190 : 230) : QColor(255, 255, 255, 5);
    if (ready) { a.setAlpha(m_down ? 150 : 210); b.setAlpha(170); }
    g.setColorAt(0, a);
    g.setColorAt(1, b);
    p.setPen(Qt::NoPen);
    p.setBrush(g);
    p.drawRoundedRect(r, rad, rad);
    p.setBrush(Qt::NoBrush);
    p.setPen(QPen(ready ? accent.lighter(m_hover ? 150 : 125) : QColor(255, 255, 255, 40), 1));
    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad);

    // grip: 2 x 3 dots
    p.setPen(Qt::NoPen);
    p.setBrush(QColor(255, 255, 255, ready ? 210 : 90));
    for (int cx = 0; cx < 2; ++cx)
        for (int cy = 0; cy < 3; ++cy) p.drawEllipse(QPointF(r.left() + 11 + cx * 5, r.center().y() - 5 + cy * 5), 1.3, 1.3);

    QFont f = font();
    f.setPointSizeF(9);
    f.setBold(true);
    p.setFont(f);
    p.setPen(ready ? QColor(255, 255, 255) : t.color("text-muted", QColor("#8b93a4")));
    const QString label = m_state == State::Ready ? "Drag audio" : m_state == State::Preparing ? "Preparing…" : "No audio";
    p.drawText(QRectF(r.left() + 27, r.top(), r.width() - 70, r.height()), Qt::AlignLeft | Qt::AlignVCenter, label);

    // format chip
    const QRectF c = chipRect();
    p.setPen(Qt::NoPen);
    p.setBrush(QColor(0, 0, 0, ready ? 70 : 40));
    p.drawRoundedRect(c, c.height() / 2, c.height() / 2);
    QFont cf = font();
    cf.setPointSizeF(8);
    cf.setBold(true);
    p.setFont(cf);
    p.setPen(ready ? accent.lighter(170) : t.color("text-disabled", QColor("#5d6475")));
    p.drawText(c, Qt::AlignCenter, m_fmt.toUpper());
}

void AudioDragHandle::mousePressEvent(QMouseEvent *e) {
    if (e->button() != Qt::LeftButton) return;
    m_down = true;
    m_dragging = false;
    m_press = e->position().toPoint();
    m_onChip = chipRect().contains(e->position());
    if (!m_onChip && m_state == State::Ready) setCursor(Qt::ClosedHandCursor);
    update();
}

void AudioDragHandle::mouseMoveEvent(QMouseEvent *e) {
    if (!m_down || m_onChip || m_dragging || m_state != State::Ready) return;
    if ((e->position().toPoint() - m_press).manhattanLength() < QApplication::startDragDistance()) return;
    const QString file = m_provider ? m_provider() : QString();
    if (file.isEmpty() || !QFileInfo::exists(file)) return;
    m_dragging = true;
    auto *mime = new QMimeData;
    mime->setUrls({QUrl::fromLocalFile(file)});
    mime->setText(QFileInfo(file).fileName());
    auto *drag = new QDrag(this);
    drag->setMimeData(mime);
    drag->setPixmap(dragPixmap(file));
    drag->setHotSpot(QPoint(20, 16));
    const Qt::DropAction act = drag->exec(Qt::CopyAction | Qt::LinkAction, Qt::CopyAction);
    m_down = false;
    setCursor(Qt::OpenHandCursor);
    emit dragged(file, act != Qt::IgnoreAction);
    update();
}

void AudioDragHandle::mouseReleaseEvent(QMouseEvent *e) {
    if (e->button() != Qt::LeftButton) return;
    const bool wasDown = m_down, wasDrag = m_dragging;
    m_down = m_dragging = false;
    if (m_state == State::Ready) setCursor(Qt::OpenHandCursor);
    update();
    if (!wasDown || wasDrag || !rect().contains(e->position().toPoint())) return;
    if (m_onChip) {                                             // click on the chip: switch format
        m_fmt = m_fmt == "wav" ? "mp3" : "wav";
        emit formatChanged(m_fmt);
        update();
        return;
    }
    if (m_state != State::Ready) return;
    const QString file = m_provider ? m_provider() : QString();
    if (file.isEmpty() || !QFileInfo::exists(file)) return;
    auto *mime = new QMimeData;                                 // click = copy the file (paste in Explorer / many editors)
    mime->setUrls({QUrl::fromLocalFile(file)});
    QGuiApplication::clipboard()->setMimeData(mime);
    emit copied(file);
}

QPixmap AudioDragHandle::dragPixmap(const QString &file) const {
    const qreal dpr = devicePixelRatioF();
    QFont f = font();
    f.setPointSizeF(9.5);
    f.setBold(true);
    const QString name = QFontMetrics(f).elidedText(QFileInfo(file).fileName(), Qt::ElideMiddle, 240);
    const int w = QFontMetrics(f).horizontalAdvance(name) + 54, h = 34;
    QPixmap pm(QSize(w, h) * dpr);
    pm.setDevicePixelRatio(dpr);
    pm.fill(Qt::transparent);
    QPainter p(&pm);
    p.setRenderHint(QPainter::Antialiasing);
    const QColor accent = ThemeManager::instance().color("accent", QColor("#4d8dff"));
    p.setPen(QPen(accent.lighter(140), 1.4));
    p.setBrush(QColor(16, 20, 34, 235));
    p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 17, 17);
    p.setPen(Qt::NoPen);
    p.setBrush(accent.lighter(130));
    for (int i = 0; i < 4; ++i) {                                // tiny waveform glyph
        const qreal bh = 6 + (i % 2 ? 10 : 4) + i * 1.5;
        p.drawRoundedRect(QRectF(14 + i * 5.5, h / 2.0 - bh / 2, 3, bh), 1.5, 1.5);
    }
    p.setFont(f);
    p.setPen(QColor(255, 255, 255));
    p.drawText(QRectF(42, 0, w - 50, h), Qt::AlignLeft | Qt::AlignVCenter, name);
    return pm;
}
