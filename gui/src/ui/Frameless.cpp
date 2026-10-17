#include "Frameless.h"

#include "Buttons.h"
#include "Config.h"
#include "GlStage.h"
#include "Icons.h"
#include "Indicators.h"
#include "ThemeManager.h"

#include <QApplication>
#include <QHBoxLayout>
#include <QLabel>
#include <QLinearGradient>
#include <QMenu>
#include <QMouseEvent>
#include <QPainter>
#include <QPainterPath>
#include <QRadialGradient>
#include <QTimer>
#include <QScreen>
#include <QToolButton>
#include <QWindow>
#include <cmath>

namespace Frameless {

Qt::Edges edgesAt(const QPoint &p, const QSize &size, int grip) {
    Qt::Edges e;
    if (p.x() < grip) e |= Qt::LeftEdge;
    if (p.x() >= size.width() - grip) e |= Qt::RightEdge;
    if (p.y() < grip) e |= Qt::TopEdge;
    if (p.y() >= size.height() - grip) e |= Qt::BottomEdge;
    return e;
}

Qt::CursorShape cursorFor(Qt::Edges e) {
    const bool l = e & Qt::LeftEdge, r = e & Qt::RightEdge, t = e & Qt::TopEdge, b = e & Qt::BottomEdge;
    if ((l && t) || (r && b)) return Qt::SizeFDiagCursor;
    if ((r && t) || (l && b)) return Qt::SizeBDiagCursor;
    if (l || r) return Qt::SizeHorCursor;
    if (t || b) return Qt::SizeVerCursor;
    return Qt::ArrowCursor;
}

bool customFrameWanted() {
    const QByteArray env = qgetenv("TTS_NATIVE_FRAME");
    if (!env.isEmpty() && env != "0") return false;
    if (Config::settings().value("nativeFrame", false).toBool()) return false;
    // Platforms without per-pixel-alpha top-level windows get the normal frame.
    static const QStringList ok{"windows", "offscreen", "xcb", "cocoa", "wayland", "minimal"};
    return ok.contains(QGuiApplication::platformName());
}

QRect fitToScreens(const QRect &saved, const QSize &fallback) {
    QRect want = saved.isValid() && !saved.isEmpty() ? saved : QRect(QPoint(100, 100), fallback);
    QScreen *best = nullptr;
    qint64 bestArea = -1;
    for (QScreen *s : QGuiApplication::screens()) {
        const QRect i = s->availableGeometry().intersected(want);
        const qint64 area = qint64(i.width()) * i.height();
        if (area > bestArea) { bestArea = area; best = s; }
    }
    if (!best) return want;
    const QRect avail = best->availableGeometry();
    if (bestArea < 120 * 120) {                                   // (almost) off every screen: centre on the primary one
        QScreen *p = QGuiApplication::primaryScreen();
        const QRect a = p ? p->availableGeometry() : avail;
        want = QRect(QPoint(0, 0), want.size().boundedTo(a.size()));
        want.moveCenter(a.center());
        return want;
    }
    want.setSize(want.size().boundedTo(avail.size()));
    if (want.right() > avail.right()) want.moveRight(avail.right());
    if (want.bottom() > avail.bottom()) want.moveBottom(avail.bottom());
    if (want.left() < avail.left()) want.moveLeft(avail.left());
    if (want.top() < avail.top()) want.moveTop(avail.top());
    return want;
}

}  // namespace Frameless

// ------------------------------------------------------------------------------------------ ShellWidget
ShellWidget::ShellWidget(bool customFrame, QWidget *parent)
    : QWidget(parent), m_custom(customFrame), m_inset(customFrame ? kWindowInset : 0) {
    setObjectName("central");
    setMouseTracking(true);
    setContentsMargins(m_inset, m_inset, m_inset, m_inset);
}

void ShellWidget::setMaximizedLook(bool maximized) {
    m_maximized = maximized;
    m_inset = (m_custom && !maximized) ? kWindowInset : 0;
    setContentsMargins(m_inset, m_inset, m_inset, m_inset);
    m_backdropSize = {};
    placeGl();
    update();
}

void ShellWidget::resizeEvent(QResizeEvent *e) {
    QWidget::resizeEvent(e);
    m_backdropSize = {};
    placeGl();
}

void ShellWidget::placeGl() {
    if (!m_gl) return;
    m_gl->setGeometry(rect().adjusted(m_inset, m_inset, -m_inset, -m_inset));
    m_gl->setCornerRadius((m_custom && !m_maximized) ? 12 : 0);
}

bool ShellWidget::setEffectsEnabled(bool on) {
    if (!on) {
        if (m_gl) {
            m_gl->hide();
            m_gl->deleteLater();
            m_gl = nullptr;
        }
        update();
        return false;
    }
    if (m_gl) return true;
    if (QGuiApplication::platformName() == "offscreen" || !qEnvironmentVariableIsEmpty("TTS_NO_GL") ||
        ThemeManager::instance().current() != "dark")
        return false;
    m_gl = new GlStage(this);
    m_gl->setAccent(ThemeManager::instance().color("accent", QColor("#3d7eff")));
    connect(m_gl, &GlStage::failed, this, [this](const QString &why) {
        setEffectsEnabled(false);
        emit effectsFailed(why);
    });
    placeGl();
    m_gl->lower();
    m_gl->show();
    QTimer::singleShot(2500, this, [this] {          // no usable context (driver missing) -> flat look
        if (m_gl && !m_gl->isValid()) {
            setEffectsEnabled(false);
            emit effectsFailed("OpenGL context could not be created");
        }
    });
    return true;
}

void ShellWidget::changeEvent(QEvent *e) {
    QWidget::changeEvent(e);
    if (e->type() == QEvent::StyleChange) m_backdropSize = {};
}

void ShellWidget::rebuildBackdrop() {
    const qreal dpr = devicePixelRatioF();
    m_backdrop = QPixmap((size() * dpr).expandedTo(QSize(1, 1)));
    m_backdrop.setDevicePixelRatio(dpr);
    m_backdrop.fill(Qt::transparent);
    QPainter p(&m_backdrop);
    p.setRenderHint(QPainter::Antialiasing);
    const auto &t = ThemeManager::instance();
    const QRectF win = QRectF(rect()).adjusted(m_inset, m_inset, -m_inset, -m_inset);
    const qreal radius = (m_custom && !m_maximized) ? 12 : 0;

    if (m_custom && m_inset > 0) {            // window drop shadow (it is part of the translucent window)
        QColor sh = t.color("shadow", QColor(0, 0, 0, 110));
        p.setPen(Qt::NoPen);
        for (int i = m_inset; i >= 1; --i) {
            QColor c = sh;
            const qreal f = 1.0 - qreal(i) / (m_inset + 1);
            c.setAlphaF(qMin(1.0, sh.alphaF() * f * f / 3.2));
            p.setBrush(c);
            p.drawRoundedRect(win.adjusted(-i, -i + 4, i, i + 4), radius + i, radius + i);
        }
    }
    QPainterPath clip;
    clip.addRoundedRect(win, radius, radius);
    p.setClipPath(clip);
    QLinearGradient g(win.topLeft(), win.bottomLeft());
    g.setColorAt(0, t.color("bg-top", t.color("bg")));
    g.setColorAt(1, t.color("bg-bottom", t.color("bg")));
    p.fillRect(win, g);

    // Perspective-inspired artwork, kept at a whisper: two soft glows and a receding floor grid.
    QColor glow = t.color("glow", QColor("#3d7eff"));
    auto radial = [&](QPointF c, qreal r, qreal a) {
        QRadialGradient rg(c, r);
        QColor c0 = glow; c0.setAlphaF(a);
        QColor c1 = glow; c1.setAlphaF(0);
        rg.setColorAt(0, c0);
        rg.setColorAt(1, c1);
        p.fillRect(win, rg);
    };
    radial(win.topLeft() + QPointF(win.width() * 0.15, 40), win.width() * 0.55, 0.10);
    radial(win.bottomRight() - QPointF(win.width() * 0.1, 20), win.width() * 0.5, 0.07);
    QColor grid = t.color("grid", glow);
    const qreal horizon = win.top() + win.height() * 0.80;
    const QPointF vp(win.center().x(), win.top() + win.height() * 0.30);
    for (int i = -14; i <= 14; ++i) {
        QColor c = grid; c.setAlphaF(0.045);
        p.setPen(QPen(c, 1));
        p.drawLine(QPointF(vp.x() + i * 14, horizon), QPointF(win.center().x() + i * win.width() / 12.0, win.bottom()));
    }
    for (int k = 0; k < 6; ++k) {
        const qreal y = horizon + (win.bottom() - horizon) * (1 - std::pow(0.62, k + 1));
        QColor c = grid; c.setAlphaF(0.05 * (k + 1) / 6.0);
        p.setPen(QPen(c, 1));
        p.drawLine(QPointF(win.left(), y), QPointF(win.right(), y));
    }
    p.setClipping(false);
    QColor edge = t.color("border-strong", QColor("#3a4050"));
    edge.setAlphaF(0.8);
    p.setPen(QPen(edge, 1));
    p.setBrush(Qt::NoBrush);
    if (m_custom) p.drawRoundedRect(win.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius);
    m_backdropSize = size();
    m_backdropTheme = t.current();
}

void ShellWidget::paintEvent(QPaintEvent *) {
    if (m_backdropSize != size() || m_backdropTheme != ThemeManager::instance().current()) rebuildBackdrop();
    QPainter p(this);
    p.setCompositionMode(QPainter::CompositionMode_Source);
    p.drawPixmap(0, 0, m_backdrop);
}

void ShellWidget::applyBaseCursor() { setCursor(m_busy ? Qt::BusyCursor : Qt::ArrowCursor); }

void ShellWidget::setBusyCursor(bool on) {
    m_busy = on;
    applyBaseCursor();
}

void ShellWidget::mouseMoveEvent(QMouseEvent *e) {
    if (m_custom && !m_maximized) {
        const Qt::Edges edges = Frameless::edgesAt(e->position().toPoint(), size());
        if (edges) setCursor(Frameless::cursorFor(edges));
        else applyBaseCursor();
    }
    QWidget::mouseMoveEvent(e);
}

void ShellWidget::leaveEvent(QEvent *e) {
    applyBaseCursor();
    QWidget::leaveEvent(e);
}

void ShellWidget::mousePressEvent(QMouseEvent *e) {
    if (m_custom && !m_maximized && e->button() == Qt::LeftButton) {
        const Qt::Edges edges = Frameless::edgesAt(e->position().toPoint(), size());
        if (edges && window()->windowHandle()) {
            window()->windowHandle()->startSystemResize(edges);       // the OS does the resizing (and snapping)
            e->accept();
            return;
        }
    }
    QWidget::mousePressEvent(e);
}

// --------------------------------------------------------------------------------------------- TitleBar
TitleBar::TitleBar(QWidget *parent) : QWidget(parent) {
    setObjectName("titleBar");
    setFixedHeight(40);
    auto *h = new QHBoxLayout(this);
    h->setContentsMargins(14, 0, 0, 0);
    h->setSpacing(10);
    m_logo = new IconWidget("app-logo", "", 20);
    m_orbSlot = new QWidget;
    m_orbSlot->setFixedSize(34, 34);
    m_orbSlot->hide();
    m_panelBtn = new QToolButton;
    m_panelBtn->setObjectName("panelToggle");
    m_panelBtn->setCheckable(true);
    m_panelBtn->setChecked(true);
    m_panelBtn->setIcon(Icons::icon("settings"));
    m_panelBtn->setIconSize(QSize(16, 16));
    m_panelBtn->setText("Tools");
    m_panelBtn->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
    m_panelBtn->setToolTip("Show or hide the voice and pronunciation panel");
    m_panelBtn->setAccessibleName("Toggle side panel");
    m_name = new QLabel("Voicely");
    m_name->setObjectName("appName");
    m_state = new StateIcon(14);
    m_chip = new QLabel("Starting…");
    m_chip->setObjectName("backendChip");
    m_menuBtn = new QToolButton;
    m_menuBtn->setObjectName("commandMenu");
    m_menuBtn->setText("Menu");
    m_menuBtn->setIcon(Icons::icon("menu"));
    m_menuBtn->setIconSize(QSize(16, 16));
    m_menuBtn->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
    m_menuBtn->setPopupMode(QToolButton::InstantPopup);
    m_menuBtn->setAccessibleName("Command menu");
    m_menuBtn->setFocusPolicy(Qt::TabFocus);
    m_menu = new QMenu(m_menuBtn);
    m_menuBtn->setMenu(m_menu);

    auto caption = [](const char *name, const char *icon, const QString &tip) {
        auto *b = new AnimatedButton;
        b->setObjectName(name);
        b->setProperty("variant", "caption");
        b->setIcon(Icons::icon(icon));
        b->setIconSize(QSize(14, 14));
        b->setToolTip(tip);
        b->setAccessibleName(tip);
        return b;
    };
    m_min = caption("captionMinimize", "window-minimize", "Minimize");
    m_max = caption("captionMaximize", "window-maximize", "Maximize");
    m_close = caption("captionClose", "window-close", "Close");
    m_close->setProperty("fxHoverColor", QColor(232, 17, 35, 230));          // conventional red close hover
    h->addWidget(m_logo);
    h->addWidget(m_orbSlot);
    h->addWidget(m_name);
    h->addSpacing(6);
    h->addWidget(m_state);
    h->addWidget(m_chip);
    h->addStretch(1);
    m_center = new QHBoxLayout;                     // the free middle of the bar: export + drag controls
    m_center->setContentsMargins(0, 0, 0, 0);
    m_center->setSpacing(8);
    h->addLayout(m_center);
    h->addStretch(1);
    h->addWidget(m_panelBtn);
    h->addWidget(m_menuBtn);
    h->addSpacing(8);
    h->addWidget(m_min);
    h->addWidget(m_max);
    h->addWidget(m_close);

    connect(m_min, &QPushButton::clicked, this, [this] { window()->showMinimized(); });
    connect(m_max, &QPushButton::clicked, this, &TitleBar::toggleMaximize);
    connect(m_close, &QPushButton::clicked, this, [this] { window()->close(); });
}

void TitleBar::addCenterWidget(QWidget *w) { m_center->addWidget(w); }

void TitleBar::setOrbMode(bool on) {
    m_logo->setVisible(!on);
    m_orbSlot->setVisible(on);
}

void TitleBar::setBackendStatus(int kind, const QString &text) {
    m_state->setKind(static_cast<StateIcon::Kind>(kind));
    m_chip->setText(text);
}

void TitleBar::syncWindowState() {
    const bool max = window()->isMaximized();
    m_max->setIcon(Icons::icon(max ? "window-restore" : "window-maximize"));
    m_max->setToolTip(max ? "Restore" : "Maximize");
    m_max->setAccessibleName(max ? "Restore" : "Maximize");
}

void TitleBar::toggleMaximize() {
    if (window()->isMaximized()) window()->showNormal();
    else window()->showMaximized();
}

void TitleBar::mousePressEvent(QMouseEvent *e) {
    // Buttons consume their own clicks, so only the empty title area arms a drag. The move starts after a small
    // movement (not on press) so a double-click can still reach mouseDoubleClickEvent.
    if (e->button() == Qt::LeftButton) {
        m_pressPos = e->position().toPoint();
        m_dragArmed = true;
        e->accept();
        return;
    }
    QWidget::mousePressEvent(e);
}

void TitleBar::mouseMoveEvent(QMouseEvent *e) {
    if (m_dragArmed && (e->buttons() & Qt::LeftButton) &&
        (e->position().toPoint() - m_pressPos).manhattanLength() >= QApplication::startDragDistance() &&
        window()->windowHandle()) {
        m_dragArmed = false;
        window()->windowHandle()->startSystemMove();
        e->accept();
        return;
    }
    QWidget::mouseMoveEvent(e);
}

void TitleBar::mouseReleaseEvent(QMouseEvent *e) {
    m_dragArmed = false;
    QWidget::mouseReleaseEvent(e);
}

void TitleBar::mouseDoubleClickEvent(QMouseEvent *e) {
    if (e->button() == Qt::LeftButton) {
        toggleMaximize();
        e->accept();
        return;
    }
    QWidget::mouseDoubleClickEvent(e);
}
